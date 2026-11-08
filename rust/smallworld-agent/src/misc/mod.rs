//! The rest (owner: **misc**): the Panel -> Git self-test (selftest.rs; mc_panel.py 502-675 + `Agent.run_selftest`,
//! `selftest_loop`), the Mojang lookup (lookup.rs; mc_agent.py 1020-1104), versions (versions.rs; `read_product`,
//! `product_state`), the request budget (budget.rs; 2588-2636), quiet failure logging (warnings.rs; `Warnings`,
//! `WARN`) and the GitHub token handling (token.rs; `TOKEN_RE`, `token_shape`, `TokenCache`).
//!
//! Daemon wiring (link's `Daemon::new`, mc_agent.py `Agent.__init__`), through [`Misc`]:
//! ```text
//! let misc = Arc::new(Misc::new(&env, &settings.state_dir, misc::github_token_fetch(link.clone()))?);
//! misc.wire(&mut panel);                       // Panel.selftest, Panel.lookup
//! // a "selftest" loop thread: misc.selftest.run_loop(&daemon.stop, &daemon.inventory_due)
//! // the inventory (data): "product": misc.product_state(), "selftest": misc.selftest.state.public()?
//! // do_lookup (actions): lookup::reply_json(&reply) (Python's key order)
//! ```

pub mod budget;
pub mod lookup;
pub mod selftest;
pub mod token;
pub mod versions;
pub mod warnings;

use crate::actions::Panel;
use crate::link::Link;
use crate::settings::Env;
use crate::types::{Obj, VERSION};
use serde_json::json;
use std::sync::Arc;

/// `Agent.github_token()`: a short-lived installation token of the panel's GitHub App, minted by the Worker
/// (which alone holds the App's key) for this agent's bearer token (`POST /agent/github-token`). The error is
/// `str(IngestError)`, which never carries a token.
pub fn github_token_fetch(link: Link) -> token::FetchFn {
    Arc::new(move || {
        link.post_json("/agent/github-token", &json!({"agent": VERSION}))
            .map_err(|e| e.to_string())
    })
}

/// What `Agent.__init__` builds for misc: the self-test (with `Agent.github`), the Mojang lookup and the
/// product version this process started with.
pub struct Misc {
    pub selftest: Arc<selftest::SelfTest>,
    pub lookup: Arc<lookup::Lookup>,
    pub product_started: Option<Obj>,
}

impl Misc {
    /// # Errors
    /// The `ValueError` texts of `github_from_env` (GITHUB_API_URL) and `lookup_urls` (MOJANG_LOOKUP_URLS):
    /// Python's `Agent(s)` raises them (the daemon dies with a traceback).
    pub fn new(
        env: &Env,
        state_dir: &std::path::Path,
        fetch_token: token::FetchFn,
    ) -> Result<Self, String> {
        let github = selftest::GhRepo::from_env(env)?;
        Ok(Self {
            selftest: Arc::new(selftest::SelfTest::new(state_dir, github, fetch_token)),
            lookup: Arc::new(lookup::Lookup::with_urls(lookup::lookup_urls(env)?)),
            product_started: versions::read_product(None),
        })
    }

    /// `Panel(selftest=self.run_selftest, lookup=Lookup(lookup_urls()))`.
    pub fn wire(&self, panel: &mut Panel) {
        let st = Arc::clone(&self.selftest);
        panel.selftest = Some(Arc::new(move |trigger: &str| st.run(trigger)));
        let lk = Arc::clone(&self.lookup);
        panel.lookup = Some(Arc::new(move |name| lk.lookup(name)));
    }

    /// `Agent.product_state()` (the inventory's `product`).
    pub fn product_state(&self) -> Option<Obj> {
        versions::product_state(versions::read_product(None), self.product_started.as_ref())
    }
}
