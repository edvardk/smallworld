//! mc_panel.py `GitHubError`, `GitHub` (ref, contents GET/PUT, refs/pulls POST, pull GET; one retry with a fresh
//! token on 401 when the token source is a cache), `github_from_env` (`CONFIG_REPO`, `CONFIG_BRANCH`,
//! `GITHUB_API_URL`). Owner: sync.
//!
//! `GitHubError` is `AgentError::Value("GitHubError", message)` (`github_error`, `is_github_error`): the sync
//! round catches it with `ValueError`, and misc's self-test tells it apart by `class_name()`. Its message is the
//! request line and the status or the transport error's class, never the token or a body.
//!
//! Tokens come from a `TokenSource`: misc's `TokenCache` (minted by the Worker, `invalidate` returns true) or
//! `FixedToken` (the self-test's `lambda: token`, no retry on 401).

use super::players::{apply_entries, dump_players, parse_players};
use super::py::{sub, type_error};
use crate::http::{HttpError, Response};
use crate::types::{AResult, AgentError, Obj, USER_AGENT, Value, py_str};
use base64::Engine as _;
use serde_json::json;
use std::sync::Arc;
use std::time::Duration;

/// `GitHubError(message)`.
pub fn github_error(message: impl Into<String>) -> AgentError {
    AgentError::Value("GitHubError".into(), message.into())
}

pub fn is_github_error(e: &AgentError) -> bool {
    matches!(e, AgentError::Value(c, _) if c == "GitHubError")
}

/// Where `GitHub.call` gets its bearer token (`GitHub.tokens`).
pub trait TokenSource: Send + Sync {
    /// `tokens()`.
    ///
    /// # Errors
    /// `GitHubError` (never containing the token).
    fn token(&self) -> AResult<String>;

    /// `isinstance(tokens, TokenCache)` + `tokens.invalidate()`: forget the token and return true if a fresh
    /// one should be tried (a cache); false for a fixed token (a 401 is then final).
    fn invalidate(&self) -> bool {
        false
    }
}

/// misc's `TokenCache` (minted by the Worker; a 401 invalidates it once).
impl TokenSource for crate::misc::token::TokenCache {
    fn token(&self) -> AResult<String> {
        self.get().map_err(|e| github_error(e.0))
    }

    fn invalidate(&self) -> bool {
        crate::misc::token::TokenCache::invalidate(self);
        true
    }
}

/// `lambda: token`.
pub struct FixedToken(pub String);

impl TokenSource for FixedToken {
    fn token(&self) -> AResult<String> {
        Ok(self.0.clone())
    }
}

/// The HTTP transport (`http::request`; tests inject a fake): method, url, headers, body, timeout, reply cap.
pub type Transport = Arc<
    dyn Fn(&str, &str, &[(&str, &str)], Option<&[u8]>, Duration, u64) -> Result<Response, HttpError>
        + Send
        + Sync,
>;

pub fn http_transport() -> Transport {
    Arc::new(crate::http::request)
}

/// `GitHub(tokens, repo, api, branch)`.
#[derive(Clone)]
pub struct GitHub {
    pub tokens: Arc<dyn TokenSource>,
    pub repo: String,
    pub api: String,
    pub branch: String,
    pub transport: Transport,
}

/// `{"number", "url", "branch", "changed"}` of `open_pr` / `open_world_pr` (`number` None: Git already has it).
#[derive(Debug, Clone, PartialEq)]
pub struct Made {
    pub number: Option<i64>,
    pub url: String,
    pub branch: String,
    pub changed: Vec<Value>,
}

impl Made {
    fn none() -> Self {
        Made {
            number: None,
            url: String::new(),
            branch: String::new(),
            changed: Vec::new(),
        }
    }
}

/// `time.strftime("%Y%m%d-%H%M%S", time.gmtime(now))`.
fn stamp(now: f64) -> String {
    let t = crate::sys::gmtime(now);
    format!(
        "{:04}{:02}{:02}-{:02}{:02}{:02}",
        t.year, t.month, t.day, t.hour, t.minute, t.second
    )
}

/// `base64.b64decode(s)` (non-strict: characters outside the alphabet are skipped; binascii's errors).
///
/// # Errors
/// `binascii.Error` / `ValueError` / `TypeError` with Python's messages.
pub fn b64decode(v: &Value) -> AResult<Vec<u8>> {
    let Value::String(s) = v else {
        return Err(type_error(&format!(
            "argument should be a bytes-like object or ASCII string, not '{}'",
            crate::worlds::py_type(v)
        )));
    };
    if !s.is_ascii() {
        return Err(AgentError::Value(
            "ValueError".into(),
            "string argument should contain only ASCII characters".into(),
        ));
    }
    let mut out = Vec::new();
    let (mut acc, mut bits, mut quad, mut pads, mut data) = (0u32, 0u32, 0usize, 0usize, 0usize);
    for b in s.bytes() {
        if b == b'=' {
            pads += 1;
            if quad >= 2 && quad + pads >= 4 {
                quad = 0;
                break;
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
        data += 1;
        acc = (acc << 6) | u32::from(val);
        bits += 6;
        quad = (quad + 1) % 4;
        if bits >= 8 {
            bits -= 8;
            out.push(u8::try_from((acc >> bits) & 0xff).unwrap_or(0));
        }
    }
    match quad {
        0 => Ok(out),
        1 => Err(AgentError::Value(
            "Error".into(),
            format!(
                "Invalid base64-encoded string: number of data characters ({data}) cannot be 1 more than a multiple of 4"
            ),
        )),
        _ => Err(AgentError::Value(
            "Error".into(),
            "Incorrect padding".into(),
        )),
    }
}

/// `bytes.decode()` (UTF-8, strict).
fn utf8(raw: Vec<u8>) -> AResult<String> {
    String::from_utf8(raw).map_err(|e| {
        let b = e.as_bytes();
        let pos = e.utf8_error().valid_up_to();
        AgentError::Value(
            "UnicodeDecodeError".into(),
            format!(
                "'utf-8' codec can't decode byte 0x{:02x} in position {pos}: invalid start byte",
                b.get(pos).copied().unwrap_or(0)
            ),
        )
    })
}

impl GitHub {
    pub fn new(tokens: Arc<dyn TokenSource>, repo: &str, api: &str, branch: &str) -> Self {
        GitHub {
            tokens,
            repo: repo.into(),
            api: api.into(),
            branch: branch.into(),
            transport: http_transport(),
        }
    }

    /// `call(method, path, body)`: GitHub's JSON reply (`{}` for an empty body).
    ///
    /// # Errors
    /// `GitHubError("{method} {path}: HTTP {code}" | "{method} {path}: {class}")`, the token source's
    /// `GitHubError`, a reply that isn't JSON (`ValueError`).
    pub fn call(&self, method: &str, path: &str, body: Option<&Value>) -> AResult<Value> {
        let data = body.map(|b| serde_json::to_vec(b).unwrap_or_default());
        let line = format!("{method} {}", path.split('?').next().unwrap_or(""));
        for attempt in [1, 2] {
            let auth = format!("Bearer {}", self.tokens.token()?);
            let mut headers = vec![
                ("Authorization", auth.as_str()),
                ("Accept", "application/vnd.github+json"),
                ("X-GitHub-Api-Version", "2022-11-28"),
                ("User-Agent", USER_AGENT),
            ];
            if data.is_some() {
                headers.push(("Content-Type", "application/json"));
            }
            let url = format!("{}{path}", self.api);
            match (self.transport)(
                method,
                &url,
                &headers,
                data.as_deref(),
                Duration::from_secs(20),
                4 << 20,
            ) {
                Ok(resp) => {
                    let raw = if resp.body.is_empty() {
                        b"{}".to_vec()
                    } else {
                        resp.body
                    };
                    let text = utf8(raw)?;
                    return smallworld_core::pyjson::loads(&text).ok_or_else(|| {
                        AgentError::Value(
                            "JSONDecodeError".into(),
                            "Expecting value: line 1 column 1 (char 0)".into(),
                        )
                    });
                }
                Err(HttpError::Status(401, _)) if attempt == 1 && self.tokens.invalidate() => {}
                Err(HttpError::Status(code, _)) => {
                    return Err(github_error(format!("{line}: HTTP {code}")));
                }
                Err(e) => return Err(github_error(format!("{line}: {}", e.class_name()))),
            }
        }
        Err(github_error(format!("{line}: HTTP 401")))
    }

    fn head(&self) -> AResult<Value> {
        let r = self.call(
            "GET",
            &format!("/repos/{}/git/ref/heads/{}", self.repo, self.branch),
            None,
        )?;
        Ok(sub(sub(&r, "object")?, "sha")?.clone())
    }

    fn contents(&self, file: &str, head: &Value) -> AResult<(String, Value)> {
        let f = self.call(
            "GET",
            &format!("/repos/{}/contents/{file}?ref={}", self.repo, py_str(head)),
            None,
        )?;
        let text = utf8(b64decode(sub(&f, "content")?)?)?;
        Ok((text, sub(&f, "sha")?.clone()))
    }

    /// `players_on_main()`: (players.yml parsed, its blob sha, main's commit sha).
    ///
    /// # Errors
    /// `call`'s, `KeyError`/`TypeError` for an unexpected reply, `parse_players`'s `ValueError`.
    pub fn players_on_main(&self) -> AResult<(Obj, Value, Value)> {
        let head = self.head()?;
        let (text, blob) = self.contents("players.yml", &head)?;
        Ok((parse_players(&text)?, blob, head))
    }

    #[allow(clippy::too_many_arguments)]
    fn branch_and_pr(
        &self,
        branch: &str,
        head: &Value,
        file: &str,
        message: &str,
        content: &str,
        blob: &Value,
        body: &str,
    ) -> AResult<(i64, String)> {
        self.call(
            "POST",
            &format!("/repos/{}/git/refs", self.repo),
            Some(&json!({"ref": format!("refs/heads/{branch}"), "sha": head})),
        )?;
        self.call(
            "PUT",
            &format!("/repos/{}/contents/{file}", self.repo),
            Some(&json!({
                "message": message,
                "content": base64::engine::general_purpose::STANDARD.encode(content.as_bytes()),
                "sha": blob,
                "branch": branch,
            })),
        )?;
        let pr = self.call(
            "POST",
            &format!("/repos/{}/pulls", self.repo),
            Some(&json!({"title": message, "head": branch, "base": self.branch, "body": body})),
        )?;
        let number = super::py::py_int(sub(&pr, "number")?)?;
        let url = get_or_null(&pr, "html_url")?;
        let url = if crate::types::truthy(url) {
            py_str(url)
        } else {
            String::new()
        };
        Ok((number, url))
    }

    /// `open_pr(entries, now)`: players.yml with `entries` applied on a new branch `panel/<ts>`, and a PR.
    ///
    /// # Errors
    /// As `call` and `players_on_main`.
    pub fn open_pr(&self, entries: &[Value], now: f64) -> AResult<Made> {
        let (players, blob, head) = self.players_on_main()?;
        let (new, changed) = apply_entries(&players, entries)?;
        if changed.is_empty() {
            return Ok(Made::none());
        }
        let branch = format!("panel/{}", stamp(now));
        let mut names = Vec::new();
        let mut who = std::collections::BTreeSet::new();
        for e in entries {
            let uuid = sub(e, "uuid")?;
            if changed.iter().any(|c| super::py::py_eq(c, uuid)) {
                let sign = if sub(e, "op")? == "add" { "+" } else { "-" };
                names.push(format!("{sign}{}", py_str(sub(e, "name")?)));
                let login = get_or_null(e, "login")?;
                who.insert(if crate::types::truthy(login) {
                    py_str(login)
                } else {
                    "?".into()
                });
            }
        }
        let message = smallworld_core::pystr::prefix_cp(
            &format!("panel: whitelist {}", names.join(" ")),
            200,
        )
        .to_string();
        let body = format!(
            "Whitelist changes made in the admin panel (already live on the server).\n\n{}\n\nBy: {}. The \
             workflow merges this by itself when only `whitelist` changed; closing it unmerged reverts the \
             change on the server.",
            names
                .iter()
                .map(|n| format!("- {n}"))
                .collect::<Vec<_>>()
                .join("\n"),
            who.into_iter().collect::<Vec<_>>().join(", ")
        );
        let content = dump_players(&new)?;
        let (number, url) = self.branch_and_pr(
            &branch,
            &head,
            "players.yml",
            &message,
            &content,
            &blob,
            &body,
        )?;
        Ok(Made {
            number: Some(number),
            url,
            branch,
            changed,
        })
    }

    /// `open_world_pr(world, now)`: server.yml with world.active/world.library replaced
    /// (`set_world_section`) on a branch `panel/world-<ts>`, and a PR.
    ///
    /// # Errors
    /// As `call`, `set_world_section`'s `ValueError`, `KeyError`/`TypeError` for a broken world record.
    pub fn open_world_pr(&self, world: &Value, now: f64) -> AResult<Made> {
        let head = self.head()?;
        let (text, blob) = self.contents("server.yml", &head)?;
        let active = sub(world, "active")?;
        let library = sub(world, "library")?;
        let new = super::world::set_world_section(&text, active, library)?;
        if new == text {
            return Ok(Made::none());
        }
        let branch = format!("panel/world-{}", stamp(now));
        let what = get_or_null(world, "what")?;
        let what = if crate::types::truthy(what) {
            py_str(what)
        } else {
            "library".into()
        };
        let message =
            smallworld_core::pystr::prefix_cp(&format!("panel: world {what}"), 200).to_string();
        let mut names = Vec::new();
        for e in crate::types::py_iter(library)? {
            names.push(py_str(sub(&e, "name")?));
        }
        let names = if names.is_empty() {
            "empty".to_string()
        } else {
            names.join(", ")
        };
        let login = get_or_null(world, "login")?;
        let login = if crate::types::truthy(login) {
            py_str(login)
        } else {
            "?".into()
        };
        let body = format!(
            "World library change made in the admin panel (already live on the server).\n\n- {what}\n- \
             world.active: `{}`\n- world.library: {names}\n\nBy: {login}. Only `world.active` and \
             `world.library` in server.yml change; the workflow merges this by itself. Closing it doesn't undo \
             the switch: deploys keep honouring the live world until server.yml says the same (switch back in \
             the panel to undo it).",
            py_str(active)
        );
        let (number, url) =
            self.branch_and_pr(&branch, &head, "server.yml", &message, &new, &blob, &body)?;
        Ok(Made {
            number: Some(number),
            url,
            branch,
            changed: Vec::new(),
        })
    }

    /// `pr_state(number)`: "merged", "open" or "closed".
    ///
    /// # Errors
    /// As `call`; `AttributeError` for a reply that isn't an object.
    pub fn pr_state(&self, number: i64) -> AResult<&'static str> {
        let pr = self.call("GET", &format!("/repos/{}/pulls/{number}", self.repo), None)?;
        let attr = |k: &str| super::py::get_attr(&pr, k).cloned();
        if crate::types::truthy(&attr("merged")?) || crate::types::truthy(&attr("merged_at")?) {
            return Ok("merged");
        }
        Ok(if attr("state")? == "open" {
            "open"
        } else {
            "closed"
        })
    }
}

fn get_or_null<'a>(v: &'a Value, key: &str) -> AResult<&'a Value> {
    super::py::get_attr(v, key)
}

/// `github_from_env(env, fetch_token)` with the token source already built (the daemon passes
/// `TokenCache::new(fetch)`): the config repo or `None`.
///
/// # Errors
/// `ValueError("GITHUB_API_URL must be https:// (http:// only for a loopback address)")`.
pub fn github_from_env(
    env: &crate::settings::Env,
    tokens: Option<Arc<dyn TokenSource>>,
) -> AResult<Option<GitHub>> {
    static REPO: std::sync::LazyLock<regex::Regex> =
        std::sync::LazyLock::new(|| regex::Regex::new(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$").unwrap());
    let e = |k: &str| env.get(k).cloned().unwrap_or_default();
    let repo = e("CONFIG_REPO");
    let Some(tokens) = tokens else {
        return Ok(None);
    };
    // re.fullmatch: `$` there is the very end (Rust's `$` without (?m) too)
    if !REPO.is_match(&repo) {
        return Ok(None);
    }
    let api_env = e("GITHUB_API_URL");
    let api = if api_env.is_empty() {
        "https://api.github.com".to_string()
    } else {
        api_env
    };
    let api = api.trim_end_matches('/').to_string();
    let (scheme, host) = split_scheme_host(&api);
    let loopback =
        scheme == "http" && matches!(host.as_deref(), Some("127.0.0.1" | "localhost" | "::1"));
    if scheme != "https" && !loopback {
        return Err(AgentError::Value(
            "ValueError".into(),
            "GITHUB_API_URL must be https:// (http:// only for a loopback address)".into(),
        ));
    }
    let branch = e("CONFIG_BRANCH");
    let branch = if branch.is_empty() {
        "main".into()
    } else {
        branch
    };
    Ok(Some(GitHub::new(tokens, &repo, &api, &branch)))
}

/// `urllib.parse.urlsplit(url)`'s scheme (lowercased) and hostname (lowercased, brackets removed).
fn split_scheme_host(url: &str) -> (String, Option<String>) {
    let Some((scheme, rest)) = url.split_once(':') else {
        return (String::new(), None);
    };
    let valid = scheme
        .chars()
        .next()
        .is_some_and(|c| c.is_ascii_alphabetic())
        && scheme
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '+' | '-' | '.'));
    if !valid {
        return (String::new(), None);
    }
    let scheme = scheme.to_ascii_lowercase();
    let Some(after) = rest.strip_prefix("//") else {
        return (scheme, None);
    };
    let netloc = after.split(['/', '?', '#']).next().unwrap_or("");
    let hostport = netloc.rsplit_once('@').map_or(netloc, |(_, h)| h);
    let host = if let Some(v6) = hostport.strip_prefix('[') {
        v6.split(']').next().unwrap_or("").to_string()
    } else {
        hostport.split(':').next().unwrap_or("").to_string()
    };
    (
        scheme,
        (!host.is_empty()).then(|| host.to_ascii_lowercase()),
    )
}

#[cfg(test)]
pub mod tests {
    use super::*;
    use std::sync::Mutex;
    use std::sync::atomic::{AtomicUsize, Ordering};

    /// method, path, JSON body, Authorization header
    pub type Seen = (String, String, Option<Value>, String);

    /// A fake GitHub: answers by "METHOD path" (prefix match), records every request (method, path, JSON body,
    /// the Authorization header).
    pub struct Fake {
        pub routes: Mutex<Vec<(String, Result<Response, HttpError>)>>,
        pub seen: Mutex<Vec<Seen>>,
    }

    impl Fake {
        pub fn new() -> Arc<Self> {
            Arc::new(Fake {
                routes: Mutex::new(Vec::new()),
                seen: Mutex::new(Vec::new()),
            })
        }

        pub fn route(&self, key: &str, status: u16, body: &Value) {
            let resp = if (200..300).contains(&status) {
                Ok(Response {
                    status,
                    body: body.to_string().into_bytes(),
                })
            } else {
                Err(HttpError::Status(status, Vec::new()))
            };
            self.routes.lock().unwrap().push((key.into(), resp));
        }

        pub fn transport(self: &Arc<Self>, api: &str) -> Transport {
            let me = self.clone();
            let api = api.to_string();
            Arc::new(move |method, url, headers, body, _t, _cap| {
                let path = url.strip_prefix(&api).unwrap_or(url).to_string();
                let auth = headers
                    .iter()
                    .find(|(k, _)| *k == "Authorization")
                    .map(|(_, v)| (*v).to_string())
                    .unwrap_or_default();
                let body = body.map(|b| serde_json::from_slice(b).unwrap());
                me.seen
                    .lock()
                    .unwrap()
                    .push((method.into(), path.clone(), body, auth));
                let key = format!("{method} {path}");
                let routes = me.routes.lock().unwrap();
                routes
                    .iter()
                    .rev()
                    .find(|(k, _)| key.starts_with(k.as_str()))
                    .map_or(Err(HttpError::Status(404, Vec::new())), |(_, r)| r.clone())
            })
        }
    }

    pub struct Counting {
        pub n: AtomicUsize,
    }

    impl TokenSource for Counting {
        fn token(&self) -> AResult<String> {
            Ok(format!("ghs_token{}", self.n.load(Ordering::SeqCst)))
        }
        fn invalidate(&self) -> bool {
            self.n.fetch_add(1, Ordering::SeqCst);
            true
        }
    }

    pub fn gh(fake: &Arc<Fake>, tokens: Arc<dyn TokenSource>) -> GitHub {
        let mut g = GitHub::new(tokens, "o/cfg", "http://127.0.0.1:1", "main");
        g.transport = fake.transport("http://127.0.0.1:1");
        g
    }

    pub fn players_routes(fake: &Fake, text: &str) {
        fake.route(
            "GET /repos/o/cfg/git/ref/heads/main",
            200,
            &json!({"object": {"sha": "abc123"}}),
        );
        let enc = base64::engine::general_purpose::STANDARD.encode(text);
        // GitHub wraps the base64 at 60 characters
        let wrapped: Vec<String> = enc
            .as_bytes()
            .chunks(60)
            .map(|c| String::from_utf8(c.to_vec()).unwrap())
            .collect();
        fake.route(
            "GET /repos/o/cfg/contents/players.yml?ref=abc123",
            200,
            &json!({"content": wrapped.join("\n") + "\n", "sha": "blob1"}),
        );
    }

    #[test]
    fn a_401_retries_once_with_a_fresh_token_from_a_cache_only() {
        let fake = Fake::new();
        fake.route("GET /repos/o/cfg/pulls/3", 401, &json!({}));
        let g = gh(
            &fake,
            Arc::new(Counting {
                n: AtomicUsize::new(0),
            }),
        );
        let e = g.pr_state(3).unwrap_err();
        assert!(is_github_error(&e));
        assert_eq!(e.to_string(), "GET /repos/o/cfg/pulls/3: HTTP 401");
        let seen = fake.seen.lock().unwrap();
        assert_eq!(seen.len(), 2);
        assert_eq!(seen[0].3, "Bearer ghs_token0");
        assert_eq!(seen[1].3, "Bearer ghs_token1");
        drop(seen);
        let fake2 = Fake::new();
        fake2.route("GET /repos/o/cfg/pulls/3", 401, &json!({}));
        let g = gh(&fake2, Arc::new(FixedToken("t".into())));
        assert!(g.pr_state(3).is_err());
        assert_eq!(fake2.seen.lock().unwrap().len(), 1);
    }

    #[test]
    fn open_pr_like_python() {
        let fake = Fake::new();
        players_routes(
            &fake,
            "whitelist:\n  - {name: \"Bob\", uuid: \"22222222-2222-4222-8222-222222222222\"}\nops: []\n",
        );
        fake.route("POST /repos/o/cfg/git/refs", 201, &json!({}));
        fake.route("PUT /repos/o/cfg/contents/players.yml", 200, &json!({}));
        fake.route(
            "POST /repos/o/cfg/pulls",
            201,
            &json!({"number": 12, "html_url": "https://github.com/o/cfg/pull/12"}),
        );
        let g = gh(&fake, Arc::new(FixedToken("t".into())));
        let entries = vec![
            json!({"uuid": "11111111-1111-4111-8111-111111111111", "name": "alice", "op": "add", "login": "a@x"}),
            json!({"uuid": "22222222-2222-4222-8222-222222222222", "name": "Bob", "op": "remove", "login": null}),
            json!({"uuid": "33333333-3333-4333-8333-333333333333", "name": "Carl", "op": "remove", "login": "z"}),
        ];
        let made = g.open_pr(&entries, 1_700_000_000.0).unwrap();
        assert_eq!(made.number, Some(12));
        assert_eq!(made.branch, "panel/20231114-221320");
        assert_eq!(made.changed.len(), 2);
        let seen = fake.seen.lock().unwrap();
        let put = seen
            .iter()
            .find(|s| s.0 == "PUT")
            .unwrap()
            .2
            .clone()
            .unwrap();
        let content = base64::engine::general_purpose::STANDARD
            .decode(put["content"].as_str().unwrap())
            .unwrap();
        assert_eq!(
            String::from_utf8(content).unwrap(),
            "whitelist:\n  - {name: \"alice\", uuid: \"11111111-1111-4111-8111-111111111111\"}\nops: []\n"
        );
        assert_eq!(put["message"], "panel: whitelist +alice -Bob");
        assert_eq!(put["sha"], "blob1");
        let pr = seen.last().unwrap().2.clone().unwrap();
        assert_eq!(
            pr["body"],
            "Whitelist changes made in the admin panel (already live on the server).\n\n- +alice\n- -Bob\n\n\
             By: ?, a@x. The workflow merges this by itself when only `whitelist` changed; closing it unmerged \
             reverts the change on the server."
        );
    }

    #[test]
    fn b64decode_like_binascii() {
        let d = |s: &str| b64decode(&json!(s)).map_err(|e| e.to_string());
        assert_eq!(d("aGVs\nbG8=\n").unwrap(), b"hello");
        assert_eq!(d("aGVsbG8").unwrap_err(), "Incorrect padding");
        assert_eq!(
            d("aGVsb").unwrap_err(),
            "Invalid base64-encoded string: number of data characters (5) cannot be 1 more than a multiple of 4"
        );
        assert_eq!(d("aGk=trailing").unwrap(), b"hi");
    }

    #[test]
    fn from_env_like_python() {
        let tok: Arc<dyn TokenSource> = Arc::new(FixedToken("t".into()));
        let env = |pairs: &[(&str, &str)]| {
            pairs
                .iter()
                .map(|(k, v)| ((*k).to_string(), (*v).to_string()))
                .collect::<crate::settings::Env>()
        };
        assert!(
            github_from_env(&env(&[("CONFIG_REPO", "o/r")]), None)
                .unwrap()
                .is_none()
        );
        assert!(
            github_from_env(&env(&[("CONFIG_REPO", "o/r/x")]), Some(tok.clone()))
                .unwrap()
                .is_none()
        );
        let g = github_from_env(
            &env(&[
                ("CONFIG_REPO", "o/r"),
                ("GITHUB_API_URL", "http://127.0.0.1:8788/"),
            ]),
            Some(tok.clone()),
        )
        .unwrap()
        .unwrap();
        assert_eq!(
            (g.api.as_str(), g.branch.as_str()),
            ("http://127.0.0.1:8788", "main")
        );
        assert!(
            github_from_env(
                &env(&[
                    ("CONFIG_REPO", "o/r"),
                    ("GITHUB_API_URL", "http://example.com")
                ]),
                Some(tok),
            )
            .is_err()
        );
    }
}
