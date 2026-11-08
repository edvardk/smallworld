//! The daemon (`mc_agent.py` with no arguments: `main()` + `Agent`; 2275-2585). Owner: **link** (wiring), each
//! loop from its module: metrics/inventory (data), logs (ship), control (link), panel sync (sync), self-test
//! (misc), client codes (data). Until every module is ported the daemon refuses to start: running it half
//! ported would answer panel commands with refusals the Python agent doesn't give.

use crate::settings::{Settings, process_env};

/// The daemon's shared state (`Agent`): settings, the stop event, the panel, the executor, the link.
pub struct Daemon {
    pub settings: Settings,
    pub stop: crate::jobs::Event,
}

/// Modules still stubs (each owner removes its name when ported; `main` refuses to start while any remain).
pub const NOT_PORTED: [&str; 6] = ["link", "ship", "data", "actions", "sync", "misc"];

/// `main()`: settings from the environment (exit 2 on a bad one), then the loops.
pub fn main() -> u8 {
    let settings = match Settings::from_env(&process_env()) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("mc-agent: {e}");
            return 2;
        }
    };
    if !NOT_PORTED.is_empty() {
        eprintln!(
            "smallworld-agent: the daemon is not yet ported to Rust (modules: {}); run mc_agent.py",
            NOT_PORTED.join(", ")
        );
        return 2;
    }
    let _ = Daemon {
        settings,
        stop: crate::jobs::Event::default(),
    };
    0
}
