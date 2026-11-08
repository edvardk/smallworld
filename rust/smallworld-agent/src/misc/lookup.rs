//! mc_agent.py `lookup_urls`, `Lookup` (1020-1104): `lookup {name}` (v1.3), a Java name -> UUID from this box,
//! not the Worker (Mojang answers Cloudflare's shared egress with 403). Only the name leaves the box; answers
//! are cached for LOOKUP_TTL_S, at most LOOKUP_PER_MINUTE requests reach Mojang per minute; no answer from
//! either endpoint is "unverified", never an error.
//!
//! The reply is a JSON object whose bytes the panel sees (`do_lookup`: `json.dumps(..., separators=(",", ":"))`
//! of an insertion-ordered dict): serialize it with [`reply_json`], not serde_json (which sorts keys).

use crate::jobs::ClockFn;
use crate::settings::{Env, check_url, url_hostname};
use crate::types::{AResult, AgentError, USER_AGENT, Value};
use serde_json::json;
use smallworld_core::pystr::prefix_cp;
use std::sync::{Arc, LazyLock, Mutex, PoisonError};
use std::time::{Duration, Instant};

pub const MOJANG_LOOKUPS: [&str; 2] = [
    "https://api.mojang.com/users/profiles/minecraft/",
    "https://api.minecraftservices.com/minecraft/profile/lookup/name/",
];
pub const LOOKUP_TIMEOUT_S: f64 = 3.0;
pub const LOOKUP_TTL_S: f64 = 600.0;
pub const LOOKUP_PER_MINUTE: usize = 20;
/// The cache holds this many entries before it drops the oldest (`len(self.cache) > 200`).
pub const LOOKUP_CACHE_MAX: usize = 200;
/// `resp.read(65536)`.
pub const LOOKUP_MAX_REPLY: u64 = 65536;

const BAD_NAME: &str = "lookup: a Java name is 3-16 letters, digits or _";

/// `lookup_urls(env)`: MOJANG_LOOKUPS, or MOJANG_LOOKUP_URLS (comma-separated; the e2e run's loopback mock,
/// http:// for loopback only, like INGEST_URL).
///
/// # Errors
/// `check_url`'s `ValueError` text for a URL it refuses.
pub fn lookup_urls(env: &Env) -> Result<Vec<String>, String> {
    let raw = env.get("MOJANG_LOOKUP_URLS").map_or("", String::as_str);
    if raw.is_empty() {
        return Ok(MOJANG_LOOKUPS.iter().map(|u| (*u).to_string()).collect());
    }
    let urls: Vec<String> = raw
        .split(',')
        .map(smallworld_core::pystr::strip_ws)
        .filter(|u| !u.is_empty())
        .map(str::to_string)
        .collect();
    for u in &urls {
        check_url(u)?;
    }
    Ok(urls)
}

/// One GET: `Ok((status, body))` for any HTTP answer (an `HTTPError` is its code with an empty body),
/// `Err(class)` for a transport failure (`URLError`, `TimeoutError`, `ValueError`).
pub type GetFn = Arc<dyn Fn(&str) -> Result<(u16, Vec<u8>), &'static str> + Send + Sync>;

/// `Lookup._get` over crate::http (Accept: application/json, the agent's User-Agent, 3 s, 64 KiB).
pub fn http_get(url: &str) -> Result<(u16, Vec<u8>), &'static str> {
    match crate::http::request(
        "GET",
        url,
        &[("Accept", "application/json"), ("User-Agent", USER_AGENT)],
        None,
        Duration::from_secs_f64(LOOKUP_TIMEOUT_S),
        LOOKUP_MAX_REPLY,
    ) {
        Ok(r) => Ok((r.status, r.body)),
        Err(crate::http::HttpError::Status(code, _)) => Ok((code, Vec::new())),
        Err(crate::http::HttpError::Transport(class)) => Err(class),
    }
}

/// `time.monotonic` (seconds since an arbitrary, fixed point).
pub fn monotonic() -> f64 {
    static BASE: LazyLock<Instant> = LazyLock::new(Instant::now);
    BASE.elapsed().as_secs_f64() + 1.0
}

#[derive(Default)]
struct State {
    /// insertion-ordered like a Python dict: re-setting a key keeps its place
    cache: Vec<(String, f64, Value)>,
    recent: Vec<f64>,
}

/// mc_agent.py `Lookup`.
pub struct Lookup {
    urls: Vec<String>,
    get: GetFn,
    clock: ClockFn,
    state: Mutex<State>,
}

fn name_ok(name: &str) -> bool {
    crate::panel::NAME.is_match(name)
}

impl Lookup {
    pub fn new(urls: Vec<String>, get: GetFn, clock: ClockFn) -> Self {
        Self {
            urls,
            get,
            clock,
            state: Mutex::new(State::default()),
        }
    }

    /// `Lookup(urls)` with the real HTTP client and `time.monotonic`.
    pub fn with_urls(urls: Vec<String>) -> Self {
        Self::new(urls, Arc::new(http_get), Arc::new(monotonic))
    }

    /// `lookup(name)`: `{found, name, uuid, via}`, `{found: false, via}`, `{found: null, unverified, detail}`,
    /// a cache hit with `cached: true`.
    ///
    /// # Errors
    /// `Rejected` for a name that isn't a Java name, and past the per-minute limit.
    pub fn lookup(&self, name: &Value) -> AResult<Value> {
        let name = match name {
            Value::String(n) if name_ok(n) => n.as_str(),
            _ => return Err(AgentError::Rejected(BAD_NAME.into())),
        };
        let (key, now) = (name.to_lowercase(), (self.clock)());
        {
            let mut st = self.state.lock().unwrap_or_else(PoisonError::into_inner);
            if let Some((_, t, hit)) = st.cache.iter().find(|(k, _, _)| *k == key)
                && now - t < LOOKUP_TTL_S
            {
                let mut out = hit.clone();
                out["cached"] = json!(true);
                return Ok(out);
            }
            st.recent.retain(|t| now - t < 60.0);
            if st.recent.len() >= LOOKUP_PER_MINUTE {
                return Err(AgentError::Rejected(format!(
                    "lookup: rate limit ({LOOKUP_PER_MINUTE} per minute)"
                )));
            }
            st.recent.push(now);
        }
        let mut result: Option<Value> = None;
        let mut failures: Vec<String> = Vec::new();
        for base in &self.urls {
            let host = url_hostname(base).unwrap_or_else(|| "?".into());
            // urllib.parse.quote(name): a Java name is all safe characters
            let (status, body) = match (self.get)(&format!("{base}{name}")) {
                Ok(r) => r,
                Err(class) => {
                    failures.push(format!("{host}: {class}"));
                    continue;
                }
            };
            if matches!(status, 204 | 404) {
                result = Some(json!({"found": false, "via": host}));
                break;
            }
            if status == 200 {
                let text = if body.is_empty() {
                    Some("{}".to_string())
                } else {
                    String::from_utf8(body).ok()
                };
                let data = text.as_deref().and_then(smallworld_core::pyjson::loads);
                let obj = data.as_ref().and_then(Value::as_object);
                let pid = obj
                    .and_then(|o| o.get("id"))
                    .filter(|v| crate::types::truthy(v))
                    .map(crate::types::py_str)
                    .unwrap_or_default()
                    .to_lowercase();
                if pid.len() == 32 && pid.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f')) {
                    let uuid = format!(
                        "{}-{}-{}-{}-{}",
                        &pid[..8],
                        &pid[8..12],
                        &pid[12..16],
                        &pid[16..20],
                        &pid[20..]
                    );
                    let shown = match obj.and_then(|o| o.get("name")) {
                        Some(Value::String(g)) if name_ok(g) => g.clone(),
                        _ => name.to_string(),
                    };
                    result = Some(json!({"found": true, "name": shown, "uuid": uuid, "via": host}));
                    break;
                }
                failures.push(format!("{host}: unexpected answer"));
                continue;
            }
            failures.push(format!("{host}: HTTP {status}"));
        }
        let Some(result) = result else {
            // not cached: the next try may get through
            return Ok(json!({
                "found": null, "unverified": true,
                "detail": prefix_cp(&failures.join("; "), 160),
            }));
        };
        let mut st = self.state.lock().unwrap_or_else(PoisonError::into_inner);
        if st.cache.len() > LOOKUP_CACHE_MAX {
            st.cache.remove(0);
        }
        if let Some(slot) = st.cache.iter_mut().find(|(k, _, _)| *k == key) {
            slot.1 = now;
            slot.2 = result.clone();
        } else {
            st.cache.push((key, now, result.clone()));
        }
        Ok(result)
    }
}

/// `json.dumps(reply, separators=(",", ":"))` with the keys in the order Python's dicts have them (found, name,
/// uuid, via, unverified, detail, cached): what `do_lookup` returns to the panel.
pub fn reply_json(reply: &Value) -> String {
    const ORDER: [&str; 7] = [
        "found",
        "name",
        "uuid",
        "via",
        "unverified",
        "detail",
        "cached",
    ];
    let Some(obj) = reply.as_object() else {
        return smallworld_core::pyjson::dumps_compact(reply);
    };
    let parts: Vec<String> = ORDER
        .iter()
        .filter_map(|k| obj.get(*k).map(|v| (*k, v)))
        .chain(
            obj.iter()
                .filter(|(k, _)| !ORDER.contains(&k.as_str()))
                .map(|(k, v)| (k.as_str(), v)),
        )
        .map(|(k, v)| {
            format!(
                "{}:{}",
                smallworld_core::pyjson::string(k, true),
                smallworld_core::pyjson::dumps_compact(v)
            )
        })
        .collect();
    format!("{{{}}}", parts.join(","))
}

#[cfg(test)]
mod tests {
    use super::*;

    type Answers = Arc<Mutex<Vec<Result<(u16, &'static str), &'static str>>>>;

    fn lookup(
        answers: &Answers,
        urls: &[&str],
        clock: &Arc<Mutex<f64>>,
    ) -> (Lookup, Arc<Mutex<Vec<String>>>) {
        let seen = Arc::new(Mutex::new(Vec::new()));
        let (a, s, c) = (answers.clone(), seen.clone(), clock.clone());
        let l = Lookup::new(
            urls.iter().map(|u| (*u).to_string()).collect(),
            Arc::new(move |url: &str| {
                s.lock().unwrap().push(url.to_string());
                let mut a = a.lock().unwrap();
                let r = if a.len() > 1 { a.remove(0) } else { a[0] };
                r.map(|(st, b)| (st, b.as_bytes().to_vec()))
            }),
            Arc::new(move || *c.lock().unwrap()),
        );
        (l, seen)
    }

    #[test]
    fn found_cached_missing_unverified_and_the_limit() {
        let answers: Answers = Arc::new(Mutex::new(vec![
            Ok((403, "")),
            Ok((
                200,
                r#"{"id": "8667BA71B85A4004AF54457A9734EED7", "name": "Steve"}"#,
            )),
        ]));
        let clock = Arc::new(Mutex::new(100.0));
        let (l, seen) = lookup(
            &answers,
            &["https://a.test/p/", "https://b.test/q/"],
            &clock,
        );
        let r = l.lookup(&json!("steve")).unwrap();
        assert_eq!(
            reply_json(&r),
            r#"{"found":true,"name":"Steve","uuid":"8667ba71-b85a-4004-af54-457a9734eed7","via":"b.test"}"#
        );
        assert_eq!(
            seen.lock().unwrap().as_slice(),
            ["https://a.test/p/steve", "https://b.test/q/steve"]
        );
        assert_eq!(
            reply_json(&l.lookup(&json!("STEVE")).unwrap()),
            r#"{"found":true,"name":"Steve","uuid":"8667ba71-b85a-4004-af54-457a9734eed7","via":"b.test","cached":true}"#
        );
        *answers.lock().unwrap() = vec![Err("URLError"), Ok((500, ""))];
        assert_eq!(
            reply_json(&l.lookup(&json!("Dave")).unwrap()),
            r#"{"found":null,"unverified":true,"detail":"a.test: URLError; b.test: HTTP 500"}"#
        );
        *answers.lock().unwrap() = vec![Ok((200, "nope")), Ok((404, ""))];
        assert_eq!(
            reply_json(&l.lookup(&json!("Dave")).unwrap()),
            r#"{"found":false,"via":"b.test"}"#
        );
        for bad in [json!("x y"), json!("ab"), json!(null), json!(5)] {
            assert_eq!(l.lookup(&bad), Err(AgentError::Rejected(BAD_NAME.into())));
        }
        // 3 reached Mojang so far; 17 more fit in the minute
        *answers.lock().unwrap() = vec![Ok((404, ""))];
        for i in 0..17 {
            assert!(l.lookup(&json!(format!("Rate_{i:02}"))).is_ok(), "{i}");
        }
        assert_eq!(
            l.lookup(&json!("Rate_99")),
            Err(AgentError::Rejected(
                "lookup: rate limit (20 per minute)".into()
            ))
        );
        *clock.lock().unwrap() = 161.0;
        assert!(l.lookup(&json!("Rate_99")).is_ok());
        // the cache expires
        *clock.lock().unwrap() = 800.0;
        assert_eq!(l.lookup(&json!("steve")).unwrap()["found"], json!(false));
    }

    #[test]
    fn urls_from_the_environment() {
        let mut env = Env::new();
        assert_eq!(lookup_urls(&env).unwrap(), MOJANG_LOOKUPS);
        env.insert(
            "MOJANG_LOOKUP_URLS".into(),
            " http://127.0.0.1:8788/a/ ,, https://x/b/".into(),
        );
        assert_eq!(
            lookup_urls(&env).unwrap(),
            ["http://127.0.0.1:8788/a/", "https://x/b/"]
        );
        env.insert("MOJANG_LOOKUP_URLS".into(), "http://10.0.0.1/".into());
        assert!(lookup_urls(&env).is_err());
    }
}
