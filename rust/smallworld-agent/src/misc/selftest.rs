//! mc_panel.py `selftest` (read-only steps token -> permissions -> repository -> main -> pulls), `SelfTestState`
//! (selftest.json; Discord after `SELFTEST_ALERT_AFTER` = 2, once more on recovery); mc_agent.py
//! `Agent.run_selftest` (cached 60 s for the panel), `selftest_loop` (daily, first run after 300 s). Owner:
//! misc. Stub.

pub const SELFTEST_S: f64 = 86400.0;
pub const SELFTEST_FIRST_S: f64 = 300.0;
pub const SELFTEST_MIN_GAP_S: f64 = 60.0;
pub const SELFTEST_ALERT_AFTER: i64 = 2;
