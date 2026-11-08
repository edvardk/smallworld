//! The rest (owner: **misc**): the Panel -> Git self-test (selftest.rs; mc_panel.py 502-675 + `Agent.run_selftest`,
//! `selftest_loop`), the Mojang lookup (lookup.rs; mc_agent.py 1020-1104), versions (versions.rs; `read_product`,
//! `product_state`), the request budget (budget.rs; 2588-2636), quiet failure logging (warnings.rs; `Warnings`,
//! `WARN`) and the GitHub token handling (token.rs; `TOKEN_RE`, `token_shape`, `TokenCache`).
//!
//! Daemon wiring (link's `Daemon`, mc_agent.py `Agent.__init__`):
//! - `selftest::SelfTest::new(state_dir, GhRepo::from_env(env)?, fetch)` with `fetch` = `Agent.github_token`
//!   (`link.post_json("/agent/github-token", {"agent": VERSION})`, the error's text); `Panel.selftest` =
//!   `|t| selftest.run(t)`; a `selftest` thread runs `selftest.run_loop(&stop, &inventory_due)`; the inventory's
//!   `selftest` is `selftest.state.public()`.
//! - `Panel.lookup` = `lookup::Lookup::with_urls(lookup_urls(env)?)`; `do_lookup` answers
//!   `lookup::reply_json(&reply)` (Python's key order).
//! - `product_started = versions::read_product(None)` at start; the inventory's `product` is
//!   `versions::product_state(versions::read_product(None), product_started.as_ref())`.

pub mod budget;
pub mod lookup;
pub mod selftest;
pub mod token;
pub mod versions;
pub mod warnings;
