//! mc_agent.py's request budget (2588-2636: `daily_budget` and its constants): Worker requests and D1 rows
//! written per day by this agent in the worst case (WebSocket down all day: polling). `tests/test_agent.py`
//! keeps `schema.budget` above Python's; the constants here equal Python's (vector test).

/// mc_agent.py `INVENTORY_S` (data's inventory loop uses the same value; pinned by the constants vector).
pub const INVENTORY_S: i64 = 600;
/// mc_agent.py `CLIENT_PER_HOUR` (data's client codes; pinned by the constants vector).
pub const CLIENT_PER_HOUR: i64 = 20;
/// P10: job reports and the inventory pushes that follow panel actions.
pub const PANEL_REQUESTS: i64 = 400;
pub const PANEL_ROWS: i64 = 100;
/// P13 (EXPERIENCE=1): one minute row per minute + one segment per 5 minutes.
pub const EXPERIENCE_REQUESTS: i64 = 1440 + 288;
pub const EXPERIENCE_ROWS: i64 = 2 * 1440 + 2 * 2 * 500 + 600;
/// v1.1 (CLIENT=1): at most CLIENT_PER_HOUR code requests per hour.
pub const CLIENT_REQUESTS: i64 = 24 * CLIENT_PER_HOUR;
pub const CLIENT_ROWS: i64 = 2 * CLIENT_REQUESTS;

/// `daily_budget(...)`'s arguments (Python's defaults in `Default`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct BudgetArgs {
    pub interval_s: i64,
    pub max_rows: i64,
    pub log_batch_s: i64,
    pub poll_s: i64,
    pub experience: bool,
    pub client: bool,
}

impl Default for BudgetArgs {
    fn default() -> Self {
        Self {
            interval_s: 60,
            max_rows: 30000,
            log_batch_s: 300,
            poll_s: 15,
            experience: true,
            client: false,
        }
    }
}

/// `daily_budget(...)`: `(requests, d1_rows)`.
pub fn daily_budget(a: BudgetArgs) -> (i64, i64) {
    let samples = 86400 / a.interval_s;
    let batches = 86400 / a.log_batch_s;
    let polls = 86400 / a.poll_s;
    let inventory = 86400 / INVENTORY_S;
    let requests = samples
        + 2 * batches
        + polls
        + inventory
        + PANEL_REQUESTS
        + if a.experience { EXPERIENCE_REQUESTS } else { 0 }
        + if a.client { CLIENT_REQUESTS } else { 0 };
    let rows = samples
        + a.max_rows
        + batches
        + inventory
        + 2 * 288
        + PANEL_ROWS
        + if a.experience { EXPERIENCE_ROWS } else { 0 }
        + if a.client { CLIENT_ROWS } else { 0 };
    (requests, rows)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::misc::{lookup, selftest};
    use crate::settings::Settings;
    use crate::types::Value;
    use serde_json::json;

    fn vectors() -> Value {
        serde_json::from_str::<Value>(include_str!(
            "../../../../tests/agent_parity/vectors/constants.json"
        ))
        .unwrap()["vectors"]
            .clone()
    }

    #[test]
    fn budget_vectors() {
        let v = vectors();
        for (name, value) in [
            ("PANEL_REQUESTS", PANEL_REQUESTS),
            ("PANEL_ROWS", PANEL_ROWS),
            ("EXPERIENCE_REQUESTS", EXPERIENCE_REQUESTS),
            ("EXPERIENCE_ROWS", EXPERIENCE_ROWS),
            ("CLIENT_REQUESTS", CLIENT_REQUESTS),
            ("CLIENT_ROWS", CLIENT_ROWS),
            ("INVENTORY_S", INVENTORY_S),
            ("CLIENT_PER_HOUR", CLIENT_PER_HOUR),
        ] {
            assert_eq!(v[name], json!(value), "{name}");
        }
        for case in v["daily_budget"].as_array().unwrap() {
            let args = &case["args"];
            let d = BudgetArgs::default();
            let int = |k: &str, dflt: i64| args.get(k).and_then(Value::as_i64).unwrap_or(dflt);
            let b = daily_budget(BudgetArgs {
                interval_s: int("interval_s", d.interval_s),
                max_rows: int("max_rows", d.max_rows),
                log_batch_s: int("log_batch_s", d.log_batch_s),
                poll_s: int("poll_s", d.poll_s),
                experience: args
                    .get("experience")
                    .and_then(Value::as_bool)
                    .unwrap_or(d.experience),
                client: args
                    .get("client")
                    .and_then(Value::as_bool)
                    .unwrap_or(d.client),
            });
            assert_eq!(
                json!({"requests": b.0, "d1_rows": b.1}),
                case["budget"],
                "{case}"
            );
        }
    }

    #[test]
    fn misc_constants_vectors() {
        let v = vectors();
        assert_eq!(v["SELFTEST_S"].as_f64(), Some(selftest::SELFTEST_S));
        assert_eq!(
            v["SELFTEST_FIRST_S"].as_f64(),
            Some(selftest::SELFTEST_FIRST_S)
        );
        assert_eq!(
            v["SELFTEST_MIN_GAP_S"].as_f64(),
            Some(selftest::SELFTEST_MIN_GAP_S)
        );
        assert_eq!(
            v["SELFTEST_ALERT_AFTER"],
            json!(selftest::SELFTEST_ALERT_AFTER)
        );
        assert_eq!(v["SELFTEST_STEPS"], json!(selftest::SELFTEST_STEPS));
        let needs: serde_json::Map<String, Value> = selftest::SELFTEST_NEEDS
            .iter()
            .map(|(k, v)| ((*k).to_string(), json!(v)))
            .collect();
        assert_eq!(v["SELFTEST_NEEDS"], Value::Object(needs));
        assert_eq!(v["MOJANG_LOOKUPS"], json!(lookup::MOJANG_LOOKUPS));
        assert_eq!(
            v["LOOKUP_TIMEOUT_S"].as_f64(),
            Some(lookup::LOOKUP_TIMEOUT_S)
        );
        assert_eq!(v["LOOKUP_TTL_S"].as_f64(), Some(lookup::LOOKUP_TTL_S));
        assert_eq!(v["LOOKUP_PER_MINUTE"], json!(lookup::LOOKUP_PER_MINUTE));
        assert_eq!(v["NAME"], json!("[A-Za-z0-9_]{3,16}"));
    }

    #[test]
    fn settings_clamps_vectors() {
        for case in vectors()["settings_clamps"].as_array().unwrap() {
            let env = case["env"]
                .as_object()
                .unwrap()
                .iter()
                .map(|(k, v)| (k.clone(), v.as_str().unwrap().to_string()))
                .collect();
            match Settings::from_env(&env) {
                Ok(s) => {
                    let got = json!({
                        "ingest_url": s.ingest_url,
                        "metrics_interval_s": s.metrics_interval_s,
                        "log_batch_s": s.log_batch_s,
                        "log_max_rows_per_day": s.log_max_rows_per_day,
                        "poll_s": s.poll_s,
                        "state_dir": s.state_dir.to_string_lossy(),
                        "panel_sync_s": s.panel_sync_s,
                        "panel_batch_s": s.panel_batch_s,
                        "panel_pr_check_s": s.panel_pr_check_s,
                        "experience": s.experience,
                        "client": s.client,
                        "client_request_dir": s.client_request_dir.to_string_lossy(),
                        "client_code_file": s.client_code_file.to_string_lossy(),
                    });
                    assert_eq!(got, case["ok"], "{case}");
                }
                Err(e) => assert_eq!(json!(e), case["error"], "{case}"),
            }
        }
    }
}
