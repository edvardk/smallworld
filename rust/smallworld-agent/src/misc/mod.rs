//! The rest (owner: **misc**): the Panel -> Git self-test (selftest.rs; mc_panel.py 502-636 + `Agent.run_selftest`,
//! `selftest_loop`), the Mojang lookup (lookup.rs; mc_agent.py 1032-1104), versions (versions.rs; `read_product`,
//! `product_state`), the request budget (budget.rs; 2588-2636), quiet failure logging (warnings.rs; `Warnings`)
//! and the GitHub token handling (token.rs; `TOKEN_RE`, `token_shape`, `TokenCache`). Not ported yet.

pub mod budget;
pub mod lookup;
pub mod selftest;
pub mod token;
pub mod versions;
pub mod warnings;
