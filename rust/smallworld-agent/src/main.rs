//! smallworld-agent: the Rust port of Smallworld's on-box agent, `scripts/mc_agent.py` plus the agent half of
//! `scripts/mc_panel.py` (and the helpers they use in-process from notify.py, worlds.py, health.py, alerts.py).
//!
//! ```text
//! smallworld-agent                                  # the daemon (mc-agent.service): Worker link, data plane
//! smallworld-agent cli mcctl:USER ACTION [ARGS_JSON] # one action for mcctl over SSH (restart, plugin, moderate,
//!                                                   # world_create|switch|delete) or a list (worlds, plugins)
//! ```
//!
//! Same command line as `mc_agent.py` (its `__main__`: `cli` as the first argument selects `cli_main(argv[2:])`,
//! anything else runs `main()`, the daemon). Python is the spec: every refusal text, audit entry, file, mode,
//! RCON line and systemctl call matches it; documented divergences are listed in PORTING.md.
//!
//! Module map and owners: PORTING.md. Status: `cli` is fully ported; the daemon's modules are interfaces with
//! stubs that report "not yet ported" (the daemon refuses to start until they are).

// unsafe is denied crate-wide (Cargo.toml) and allowed only in sys.rs, block by block, for libc calls std lacks.
#![deny(unsafe_code)]
// The daemon's modules are interfaces whose callers land with them (PORTING.md); until the daemon is wired up,
// much of the tree is unused. Remove this with the integration step.
#![allow(dead_code)]
// Stubs take `self` and arguments they don't use yet; the same integration step removes these two.
#![allow(clippy::unused_self, clippy::needless_pass_by_value)]
// Python names (argv/args, job/jobs) are kept on purpose so the port reads side by side with the original.
#![allow(clippy::similar_names, clippy::too_many_lines)]

mod actions;
mod cli;
mod daemon;
mod data;
mod http;
mod jobs;
mod link;
mod misc;
mod notify;
mod panel;
mod settings;
mod ship;
mod sync;
mod sys;
mod systemd;
mod types;
mod unicode;
mod worlds;

use std::os::unix::ffi::OsStringExt;
use std::process::ExitCode;

fn main() -> ExitCode {
    // `os.umask(0o077)` before anything else: spool, cursor, nonces and pending.json stay private.
    sys::umask(0o077);
    // Python decodes argv with surrogateescape; our arguments are ASCII logins, action names and JSON, so a
    // lossy decode only differs for bytes Python would also refuse (they can't match CLI_LOGIN or parse).
    let argv: Vec<String> = std::env::args_os()
        .map(|a| String::from_utf8_lossy(&a.into_vec()).into_owned())
        .collect();
    let code = if argv.get(1).map(String::as_str) == Some("cli") {
        cli::main(&argv[2..])
    } else {
        daemon::main()
    };
    ExitCode::from(code)
}
