//! Whitelist -> Git (owner: **sync**; mc_panel.py 117-189, 291-501, 639-884): players.yml canonical form
//! (players.rs; equals mcctl.schema.dump_players), the GitHub REST client (github.rs; contents + pulls, one
//! retry with a fresh token on 401), the sync round (round.rs: snapshot, expire after 24 h, PR check, open a
//! PR after BATCH_S, `sync_world` with worlds.py's `set_world_section` (world.rs), `merge_sync`, `revert_live`),
//! the Discord texts (alerts.rs; byte-exact `alerts.render`) and `Agent.panel_loop` (here).

pub mod alerts;
pub mod github;
pub mod players;
pub mod py;
pub mod round;
pub mod world;

use crate::jobs::Event;
use crate::panel::pending::Pending;
use crate::settings::{Env, Settings};
use crate::types::AResult;
use github::{GitHub, TokenSource};
use std::panic::{AssertUnwindSafe, catch_unwind};
use std::sync::Arc;

/// What `Agent.panel_loop` uses: the config repo (or none), the timings and the inventory trigger.
pub struct PanelSync {
    pub github: Option<GitHub>,
    pub sync_s: f64,
    pub batch_s: f64,
    pub pr_check_s: f64,
    /// `inventory_due.set`.
    pub changed: Arc<dyn Fn() + Send + Sync>,
}

impl PanelSync {
    /// `Agent.__init__`'s `github_from_env(fetch_token=self.github_token)` (the daemon passes misc's
    /// `TokenCache` over link's `POST /agent/github-token`) and the PANEL_* settings.
    ///
    /// # Errors
    /// `github_from_env`'s `ValueError` (a GITHUB_API_URL that isn't https or loopback http).
    pub fn new(
        settings: &Settings,
        env: &Env,
        tokens: Option<Arc<dyn TokenSource>>,
        changed: Arc<dyn Fn() + Send + Sync>,
    ) -> AResult<Self> {
        #[allow(clippy::cast_precision_loss)]
        Ok(PanelSync {
            github: github::github_from_env(env, tokens)?,
            sync_s: settings.panel_sync_s as f64,
            batch_s: settings.panel_batch_s as f64,
            pr_check_s: settings.panel_pr_check_s as f64,
            changed,
        })
    }

    /// One round with the product's alert (Discord), audit and RCON.
    ///
    /// # Errors
    /// What `sync_pending` doesn't catch.
    pub fn round(&self, pending: &Pending, now: f64) -> AResult<bool> {
        let alert = |m: &str| crate::notify::discord(m);
        let log = |m: &str, action: &str, result: &str| {
            smallworld_core::audit::audit(m, &[("action", action), ("result", result)]);
        };
        let rcon = |l: &str| crate::jobs::rcon_command(l);
        round::sync_pending(
            pending,
            self.github.as_ref(),
            now,
            &round::Round {
                alert: &alert,
                log: &log,
                rcon: &rcon,
                batch_s: self.batch_s,
                pr_check_s: self.pr_check_s,
            },
        )
    }
}

/// `Agent.panel_loop`: every PANEL_SYNC_S until `stop`, one round; a failed round is printed and the loop goes
/// on (`panel sync failed: <class>: <message>`).
pub fn panel_loop(p: &PanelSync, pending: &Pending, stop: &Event) {
    while !stop.wait(p.sync_s) {
        match catch_unwind(AssertUnwindSafe(|| p.round(pending, crate::sys::now()))) {
            Ok(Ok(true)) => (p.changed)(),
            Ok(Ok(false)) => {}
            Ok(Err(e)) => eprintln!("panel sync failed: {}", py::exc_line(&e)),
            Err(panic) => {
                let why = panic
                    .downcast_ref::<String>()
                    .cloned()
                    .or_else(|| panic.downcast_ref::<&str>().map(|s| (*s).to_string()))
                    .unwrap_or_default();
                eprintln!("panel sync failed: RuntimeError: {why}");
            }
        }
    }
}
