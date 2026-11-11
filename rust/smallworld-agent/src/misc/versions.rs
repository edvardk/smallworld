//! mc_agent.py `read_product` (1008-1017: the product version the last deploy installed, regex-checked from
//! product-version.json), `Agent.product_started` (read once at start: a deploy that installs new code
//! restarts the agent, so a difference from the file means it didn't) and `Agent.product_state` (2387-2392:
//! installed + running, for the inventory).

use crate::types::{Obj, Value};
use regex::Regex;
use serde_json::json;
use smallworld_core::paths;
use smallworld_core::pystr::PyStr;
use std::path::Path;
use std::sync::LazyLock;

static VERSION_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[0-9A-Za-z.+-]{1,40}$").expect("a constant pattern"));
static COMMIT_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[0-9a-f]{40}$").expect("a constant pattern"));
static FINGERPRINT_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[0-9a-f]{64}$").expect("a constant pattern"));

/// mc_agent.py `read_json_obj(path)`: the file's JSON object through `read_untrusted` (no FIFO, no symlink,
/// capped), `{}` on any OSError/ValueError or a non-object.
pub fn read_json_obj(path: &Path) -> Obj {
    let Ok(raw) =
        smallworld_core::properties::read_untrusted(&PyStr::from(path.to_string_lossy().as_ref()))
    else {
        return Obj::new();
    };
    // json.loads(bytes) skips a UTF-8 BOM
    let raw = raw.strip_prefix(b"\xef\xbb\xbf").unwrap_or(&raw);
    match std::str::from_utf8(raw)
        .ok()
        .and_then(smallworld_core::pyjson::loads)
    {
        Some(Value::Object(o)) => o,
        _ => Obj::new(),
    }
}

/// `read_product(path)`: `{version, commit[, worker_fingerprint]}` or None (no file yet: a box deployed before v1.3, or not by the
/// product). A commit that isn't 40 hex digits reads as "".
pub fn read_product(path: Option<&Path>) -> Option<Obj> {
    let data = read_json_obj(path.unwrap_or(Path::new(paths::PRODUCT_VERSION_FILE)));
    let version = match data.get("version") {
        Some(Value::String(v)) if VERSION_RE.is_match(v) => v.clone(),
        _ => return None,
    };
    let commit = match data.get("commit") {
        Some(Value::String(c)) if COMMIT_RE.is_match(c) => c.clone(),
        _ => String::new(),
    };
    let mut out = json!({"version": version, "commit": commit})
        .as_object()
        .cloned()?;
    // v1.5.2: the Worker fingerprint the release expects, only when the file has a valid one (like Python)
    if let Some(Value::String(w)) = data.get("worker_fingerprint")
        && FINGERPRINT_RE.is_match(w)
    {
        out.insert("worker_fingerprint".into(), json!(w));
    }
    Some(out)
}

/// `Agent.product_state()`: the installed version (`installed`, read now) and the one this process started
/// with (`started`, read at start), or None when nothing is installed.
pub fn product_state(installed: Option<Obj>, started: Option<&Obj>) -> Option<Obj> {
    let mut out = installed?;
    let field = |k: &str| {
        started
            .and_then(|s| s.get(k))
            .filter(|v| crate::types::truthy(v))
            .map(crate::types::py_str)
            .unwrap_or_default()
    };
    out.insert("running".into(), json!(field("version")));
    out.insert("running_commit".into(), json!(field("commit")));
    Some(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn product_version_like_python() {
        let d = std::env::temp_dir().join(format!("swagent-versions-{}", std::process::id()));
        std::fs::create_dir_all(&d).unwrap();
        let f = d.join("product-version.json");
        assert_eq!(read_product(Some(&f)), None);
        let commit = "0123456789abcdef0123456789abcdef01234567";
        std::fs::write(
            &f,
            format!(r#"{{"version": "1.4.0", "commit": "{commit}", "x": 1}}"#),
        )
        .unwrap();
        let p = read_product(Some(&f)).unwrap();
        assert_eq!(
            Value::Object(p.clone()),
            json!({"version": "1.4.0", "commit": commit})
        );
        std::fs::write(&f, r#"{"version": "1.4.0", "commit": "ABC"}"#).unwrap();
        assert_eq!(read_product(Some(&f)).unwrap()["commit"], json!(""));
        let fp = "d".repeat(64);
        std::fs::write(
            &f,
            format!(
                r#"{{"version": "1.5.2", "commit": "{commit}", "worker_fingerprint": "{fp}"}}"#
            ),
        )
        .unwrap();
        assert_eq!(
            Value::Object(read_product(Some(&f)).unwrap()),
            json!({"version": "1.5.2", "commit": commit, "worker_fingerprint": fp})
        );
        for bad in [r#""""#, r#""DD""#, "64", r#""ddd""#] {
            std::fs::write(
                &f,
                format!(r#"{{"version": "1.5.2", "worker_fingerprint": {bad}}}"#),
            )
            .unwrap();
            assert!(
                !read_product(Some(&f))
                    .unwrap()
                    .contains_key("worker_fingerprint"),
                "{bad}"
            );
        }
        for bad in [
            r#"{"version": "1 4"}"#,
            r#"{"version": 14}"#,
            "[1]",
            "{bad",
            r#"{"version": ""}"#,
        ] {
            std::fs::write(&f, bad).unwrap();
            assert_eq!(read_product(Some(&f)), None, "{bad}");
        }
        let started = json!({"version": "1.3.1", "commit": ""})
            .as_object()
            .cloned()
            .unwrap();
        let st = product_state(Some(p.clone()), Some(&started)).unwrap();
        assert_eq!(
            Value::Object(st),
            json!({"version": "1.4.0", "commit": commit, "running": "1.3.1", "running_commit": ""})
        );
        let st = product_state(Some(p), None).unwrap();
        assert_eq!(st["running"], json!(""));
        assert_eq!(product_state(None, Some(&started)), None);
        let _ = std::fs::remove_dir_all(&d);
    }
}
