//! mc_panel.py `TOKEN_RE` (`[A-Za-z0-9_.-]{20,4096}`), `token_shape` (why a token was refused, never the token)
//! and `TokenCache` (panel:315-372): short-lived GitHub App installation tokens minted by the Worker
//! (`POST /agent/github-token`), reused until 5 minutes before `expires_at`, kept in memory only;
//! `invalidate` after a 401.

use crate::jobs::ClockFn;
use crate::types::{Value, truthy};
use crate::worlds::py_type;
use regex::Regex;
use smallworld_core::pyjson::{py_str, repr_str};
use smallworld_core::pystr::{len_cp, prefix_cp};
use std::sync::{Arc, LazyLock, Mutex, PoisonError};

/// mc_panel.py `TOKEN_RE`'s pattern.
pub const TOKEN_PATTERN: &str = "^[A-Za-z0-9_.-]{20,4096}$";
/// A token is reused while it has more than this many seconds left.
pub const REUSE_MARGIN_S: f64 = 300.0;
/// The validity assumed when `expires_at` doesn't parse.
pub const FALLBACK_TTL_S: f64 = 1800.0;

static TOKEN_RE: LazyLock<Regex> = LazyLock::new(|| {
    regex::RegexBuilder::new(TOKEN_PATTERN)
        .size_limit(1 << 24)
        .build()
        .expect("a constant pattern")
});

/// `TOKEN_RE.fullmatch(token)`.
pub fn token_ok(token: &str) -> bool {
    TOKEN_RE.is_match(token)
}

/// `token_shape(data, token)`: the reply's keys, the token's length and its unexpected characters (sorted,
/// at most 8, as `repr`), never the token itself.
pub fn token_shape(data: &Value, token: &str) -> String {
    let Value::Object(obj) = data else {
        return format!("reply is {}", py_type(data));
    };
    if token.is_empty() {
        let err = obj.get("error").filter(|v| truthy(v)).map(py_str);
        let err = prefix_cp(err.as_deref().unwrap_or(""), 120);
        let keys: Vec<&str> = obj.keys().map(String::as_str).collect(); // sorted (code points)
        let keys = if keys.is_empty() {
            "none".to_string()
        } else {
            keys.join(", ")
        };
        let tail = if err.is_empty() {
            String::new()
        } else {
            format!(": {err}")
        };
        return format!("no token in the reply (keys: {keys}){tail}");
    }
    let mut odd: Vec<char> = token
        .chars()
        .filter(|c| !(c.is_ascii_alphanumeric() || matches!(c, '_' | '.' | '-')))
        .collect();
    odd.sort_unstable();
    odd.dedup();
    let shown: String = odd.iter().take(8).collect();
    let extra = if odd.is_empty() {
        String::new()
    } else {
        format!(", unexpected {}", repr_str(&shown))
    };
    format!("{} characters{extra}", len_cp(token))
}

static EXPIRES_RE: LazyLock<Regex> = LazyLock::new(|| {
    // time.strptime(s, "%Y-%m-%dT%H:%M:%S"): _strptime's regexes for these directives, IGNORECASE, Unicode \d.
    Regex::new(
        r"(?i)^(\d\d\d\d)-(1[0-2]|0[1-9]|[1-9])-(3[01]|[12]\d|0[1-9]|[1-9]| [1-9])T(2[0-3]|[0-1]\d|\d):([0-5]\d|\d):(6[0-1]|[0-5]\d|\d)$",
    )
    .expect("a constant pattern")
});

fn days_from_civil(y: i64, m: i64, d: i64) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let mp = (m + 9) % 12;
    let doy = (153 * mp + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

fn days_in_month(y: i64, m: i64) -> i64 {
    match m {
        2 if (y % 4 == 0 && y % 100 != 0) || y % 400 == 0 => 29,
        2 => 28,
        4 | 6 | 9 | 11 => 30,
        _ => 31,
    }
}

/// `float(calendar.timegm(time.strptime(text[:19], "%Y-%m-%dT%H:%M:%S")))`, or None for Python's `ValueError`.
pub fn parse_expires(text: &str) -> Option<f64> {
    let caps = EXPIRES_RE.captures(prefix_cp(text, 19))?;
    let num = |i: usize| crate::unicode::parse_decimal(caps[i].trim_start());
    let (year, month, day) = (num(1)?, num(2)?, num(3)?);
    let (hour, minute, second) = (num(4)?, num(5)?, num(6)?);
    // datetime_date(year, month, day): year >= 1, a real day of that month
    if year < 1 || day > days_in_month(year, month) {
        return None;
    }
    #[allow(clippy::cast_precision_loss)]
    Some((days_from_civil(year, month, day) * 86400 + hour * 3600 + minute * 60 + second) as f64)
}

/// The Worker's reply to `POST /agent/github-token` (`Err`: `str(e)` of whatever the fetch raised).
pub type FetchFn = Arc<dyn Fn() -> Result<Value, String> + Send + Sync>;

/// mc_panel.py `GitHubError` as `TokenCache` raises it (sync's GitHub client carries the same text).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TokenError(pub String);

impl std::fmt::Display for TokenError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

/// mc_panel.py `TokenCache`.
pub struct TokenCache {
    fetch: FetchFn,
    clock: ClockFn,
    /// (token, expires)
    held: Mutex<(String, f64)>,
}

impl TokenCache {
    pub fn new(fetch: FetchFn, clock: ClockFn) -> Self {
        Self {
            fetch,
            clock,
            held: Mutex::new((String::new(), 0.0)),
        }
    }

    /// `TokenCache(fetch)` with `time.time`.
    pub fn with_fetch(fetch: FetchFn) -> Self {
        Self::new(fetch, Arc::new(crate::sys::now))
    }

    /// `cache()`: a token with more than 5 minutes left, fetching a new one when needed.
    ///
    /// # Errors
    /// `TokenError("no GitHub token from the Worker: ...")` when the fetch fails, `TokenError("the Worker
    /// returned no usable GitHub token (<token_shape>)")` for a reply without a usable token.
    pub fn get(&self) -> Result<String, TokenError> {
        {
            let held = self.held.lock().unwrap_or_else(PoisonError::into_inner);
            if !held.0.is_empty() && held.1 - (self.clock)() > REUSE_MARGIN_S {
                return Ok(held.0.clone());
            }
        }
        let data = (self.fetch)().map_err(|e| {
            TokenError(format!(
                "no GitHub token from the Worker: {}",
                prefix_cp(&e, 160)
            ))
        })?;
        let token = match &data {
            Value::Object(o) => o
                .get("token")
                .filter(|v| truthy(v))
                .map(py_str)
                .unwrap_or_default(),
            _ => String::new(),
        };
        if !token_ok(&token) {
            return Err(TokenError(format!(
                "the Worker returned no usable GitHub token ({})",
                token_shape(&data, &token)
            )));
        }
        let raw = data.get("expires_at").map_or(String::new(), py_str);
        let expires = parse_expires(&raw).unwrap_or_else(|| (self.clock)() + FALLBACK_TTL_S);
        *self.held.lock().unwrap_or_else(PoisonError::into_inner) = (token.clone(), expires);
        Ok(token)
    }

    /// `invalidate()`.
    pub fn invalidate(&self) {
        *self.held.lock().unwrap_or_else(PoisonError::into_inner) = (String::new(), 0.0);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn vectors() -> Value {
        serde_json::from_str::<Value>(include_str!(
            "../../../../tests/agent_parity/vectors/tokens.json"
        ))
        .unwrap()["vectors"]
            .clone()
    }

    #[test]
    fn token_re_vectors() {
        let v = vectors();
        assert_eq!(
            format!("^{}$", v["token_re"].as_str().unwrap()),
            TOKEN_PATTERN
        );
        for case in v["fullmatch"].as_array().unwrap() {
            let t = case["token"].as_str().unwrap();
            assert_eq!(
                token_ok(t),
                case["match"].as_bool().unwrap(),
                "{}",
                prefix_cp(t, 40)
            );
        }
        assert_eq!(v["reuse_margin_s"], json!(300));
    }

    #[test]
    fn token_shape_vectors() {
        for case in vectors()["token_shape"].as_array().unwrap() {
            let token = case["token"].as_str().unwrap();
            assert_eq!(
                token_shape(&case["data"], token),
                case["shape"].as_str().unwrap(),
                "{case}"
            );
        }
    }

    #[test]
    fn expires_at_vectors() {
        for case in vectors()["expires_at"].as_array().unwrap() {
            let got = parse_expires(case["expires_at"].as_str().unwrap());
            match &case["expires"] {
                Value::String(s) => assert!(
                    s.starts_with("fallback") && got.is_none(),
                    "{case}: {got:?}"
                ),
                n => assert_eq!(got, n.as_f64(), "{case}"),
            }
        }
        assert_eq!(parse_expires("2026-11-08t12:00:00"), Some(1_794_139_200.0));
        assert_eq!(parse_expires("2024-02-29T00:00:00"), Some(1_709_164_800.0));
        assert_eq!(parse_expires("2023-02-29T00:00:00"), None);
        assert_eq!(parse_expires("0000-01-01T00:00:00"), None);
    }

    #[test]
    fn cache_reuses_until_five_minutes_before_expiry() {
        let calls = Arc::new(Mutex::new(0));
        let now = Arc::new(Mutex::new(1_794_139_200.0 - 1000.0));
        let (c2, n2) = (calls.clone(), now.clone());
        let cache = TokenCache::new(
            Arc::new(move || {
                *c2.lock().unwrap() += 1;
                Ok(
                    json!({"token": "ghs_".to_string() + &"a".repeat(36), "expires_at": "2026-11-08T12:00:00Z"}),
                )
            }),
            Arc::new(move || *n2.lock().unwrap()),
        );
        assert!(cache.get().is_ok());
        assert!(cache.get().is_ok());
        assert_eq!(*calls.lock().unwrap(), 1);
        *now.lock().unwrap() = 1_794_139_200.0 - 300.0;
        assert!(cache.get().is_ok());
        assert_eq!(*calls.lock().unwrap(), 2);
        cache.invalidate();
        *now.lock().unwrap() = 0.0;
        assert!(cache.get().is_ok());
        assert_eq!(*calls.lock().unwrap(), 3);
    }

    #[test]
    fn errors_never_carry_the_token() {
        let secret = "ghs_secret!secret!secret!";
        let cache = TokenCache::with_fetch(Arc::new(move || Ok(json!({"token": secret}))));
        let e = cache.get().unwrap_err().0;
        assert_eq!(
            e,
            "the Worker returned no usable GitHub token (25 characters, unexpected '!')"
        );
        let cache =
            TokenCache::with_fetch(Arc::new(
                || Err("POST /agent/github-token: HTTP 502".into()),
            ));
        assert_eq!(
            cache.get().unwrap_err().0,
            "no GitHub token from the Worker: POST /agent/github-token: HTTP 502"
        );
        let cache = TokenCache::with_fetch(Arc::new(|| Ok(json!({"token": 12_345}))));
        assert_eq!(
            cache.get().unwrap_err().0,
            "the Worker returned no usable GitHub token (5 characters)"
        );
    }
}
