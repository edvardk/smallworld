//! `read_json_obj`, `read_json_list`, `_clip`, `collect_inventory`, `push_inventory`, `Agent.panel_state`,
//! `product_state` wiring (mc_agent.py 871-937, 2405-2431). Owner: data. Stub.

use crate::types::{AResult, AgentError, Value};

/// `collect_inventory(server_dir)`.
///
/// # Errors
/// `NotPorted`.
pub fn collect_inventory() -> AResult<Value> {
    Err(AgentError::NotPorted("data::inventory::collect_inventory"))
}
