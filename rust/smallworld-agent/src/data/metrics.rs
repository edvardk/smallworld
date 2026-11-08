//! `Counters`, `parse_net_dev`, `parse_meminfo`, `cpu_busy_pct`, `last_heap`, `unit_memory_mb`, `unit_uptime_s`,
//! `parse_cores`, `parse_meminfo_mb`, `parse_tps_all`, `player_names`, `rcon_stats`, `collect`, `Spool`,
//! `push_metrics` (mc_agent.py 299-595). ~60 fields with Python's `round()` (pyround: `format!("{:.n$}")`
//! matched Python on a tie corpus). Owner: data. Stub.

use crate::types::{AResult, AgentError, Value};

/// mc_agent.py `MAX_CORES`.
pub const MAX_CORES: usize = 32;
pub const METRICS_BATCH: usize = 200;
pub const SPOOL_MAX_AGE_S: f64 = 86400.0;

/// `Counters`: the previous sample's counters (rates between samples).
#[derive(Debug, Default, Clone)]
pub struct Counters;

/// `collect(counters, now, server_dir)`: one wide sample.
///
/// # Errors
/// `NotPorted`.
pub fn collect(_counters: &mut Counters) -> AResult<Value> {
    Err(AgentError::NotPorted("data::metrics::collect"))
}

/// Python's `round(x, n)` (round-half-even on the binary value, as `format!("{:.n$}")` does). Stub until data
/// pins it with vectors.
pub fn py_round(x: f64, _n: usize) -> f64 {
    x
}
