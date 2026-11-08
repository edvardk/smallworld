//! mc_agent.py `handle_command` (1946-1969): verify, check, run, audit; always a result object
//! `{id, ok, output|error, finished_at}`. Audit: `agent action={action or '?'} ok={True|False}
//! login={login or '?'}` with MC_ACTION/LOGIN/ARGS (`json.dumps(args, sort_keys=True)[:500]`, pyjson)/RESULT.
//! A `NotPorted` from the Executor is answered as `Rejected("{action}: not yet ported to Rust")`. Owner: link.

use super::verify::Nonces;
use crate::actions::Executor;
use crate::settings::Settings;
use crate::types::Value;
use serde_json::json;

/// `handle_command(message, s, nonces, executor)`. Stub: refuses everything (audited as such once ported).
pub fn handle_command(
    message: &Value,
    _s: &Settings,
    _nonces: &mut Nonces,
    _executor: &mut Executor,
) -> Value {
    let _ = message;
    json!({"id": "", "ok": false, "error": "the Rust agent's command path is not yet ported"})
}
