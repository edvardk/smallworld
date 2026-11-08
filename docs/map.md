# Where things are

A map for finding code fast: what the labels in comments and docs mean, then each concern with its main code,
tests, docs and the CLAUDE.md rule that governs it. Every backticked path here exists and every top-level
`mcctl` command is listed (`tests/test_docs.py` checks both). Files in a deployment's config repo
(server.yml, players.yml, the lock files, .env) are written without backticks: they are not in this repo.

## Labels

The design notes named below (product-v1, client-v1.x) are the maintainer's development history and
are not published with the product; the labels stay in comments and commit messages.

| Label | Means | Where it is defined |
|---|---|---|
| P1 … P18, "Profile" | The product's implementation phases. `phase-N:` commits and "Pn" in comments, docstrings and test names refer to these | product-v1.md §8 (the phase table) |
| Profile | The phase between P16 and P17: `performance.profile` (vanilla \| balanced), tracking ranges, busiest-chunks telemetry, alert template + repeat policy | §8 row "Profile"; [operations: Performance profile](operations.md#performance-profile) |
| §N, "design §N" | A section of the product design | product-v1.md |
| v1.1 | The client modpack, its setup page, the artifact mirror, `/view` | client-v1.1.md; CHANGELOG `[1.1.0]` |
| v1.2 | The next release: work since v1.1.0 | [CHANGELOG `[Unreleased]`](../CHANGELOG.md#unreleased), client-v1.2.md |
| "P10 review", "P12 review", "security review M2/H2" | Fixes after a phase's review, written up in that phase's notes in §8 and in SECURITY.md | §8, [SECURITY.md](../SECURITY.md) |

## mcctl commands

All commands are parsed in `mcctl/cli.py` (`build_parser`); the `cmd_*` function calls the module below.

| Command | Module | Tests |
|---|---|---|
| `mcctl` (no command: the home screen, v1.3), `-C` remembered | `mcctl/home.py`, `mcctl/homecfg.py` (~/.config/mcctl/config.toml) | `tests/test_home.py` |
| Update notice (v1.3: every interactive command and the home screen's "Update" row) | `mcctl/notice.py` (+ `mcctl/updates.py` `latest_tag`) | `tests/test_notice.py` |
| `mcctl validate` | `mcctl/configrepo.py`, `mcctl/schema.py` | `tests/test_mcctl_schema.py`, `tests/test_configrepo.py` |
| `mcctl render` | `mcctl/render.py` | `tests/test_mcctl_render.py`, `tests/test_overlays.py` |
| `mcctl schema` | `mcctl/schema.py` → `schema/server.schema.json` | `tests/test_mcctl_schema.py` |
| `mcctl deploy` | `mcctl/deploy.py`, `mcctl/galaxy.py`, `mcctl/secrets.py` | `tests/test_mcctl_deploy.py`, `tests/test_galaxy.py` |
| `mcctl plan`, `mcctl apply` | `mcctl/tf.py` (re-exec from the pin: `mcctl/product.py`) | `tests/test_mcctl_tf.py`, `tests/test_mcctl_reexec.py` |
| `mcctl bootstrap` | `mcctl/bootstrap.py`, `ansible/bootstrap.yml` | `tests/test_mcctl_tf.py` |
| `mcctl doctor` | `mcctl/doctor.py`, `mcctl/providers/`, `mcctl/expiry.py`, `mcctl/pinmove.py` | `tests/test_mcctl_doctor.py`, `tests/test_mcctl_providers.py`, `tests/test_pinmove.py` |
| `mcctl status`, `mcctl rcon`, `mcctl ssh`, `mcctl logs`, `mcctl backup` | `mcctl/remote.py` | `tests/test_mcctl_core.py` |
| `mcctl stop`, `mcctl start` | `mcctl/remote.py` → `scripts/admin.py` | `tests/test_admin.py` |
| `mcctl restart`, `mcctl console`, `mcctl worlds`, `mcctl plugins` (v1.3: the panel's actions over the tailnet) | `mcctl/actions.py` → `scripts/mc_agent.py` (`cli_main`: the Executor, audit), `scripts/mc_panel.py` (`Jobs`, `JOB_LOCK_FILE`) | `tests/test_mcctl_actions.py`, `tests/test_agent_cli.py`, `tests/test_home.py` |
| `mcctl metrics` | `mcctl/metrics.py` | `tests/test_mcctl_metrics.py` |
| `mcctl init`, `mcctl setup` | `mcctl/init.py` (17 steps, `STEPS`), `mcctl/mock.py`, `mcctl/state.py`, `mcctl/ui.py` | `tests/test_mcctl_init.py`, `tests/test_setup_resume.py`, `tests/test_setup_automation.py`, `tests/test_ui.py`, `tests/e2e/answers.yml` |
| `mcctl product-setup` | `mcctl/productsetup.py` | `tests/test_setup_automation.py`, **`tests/test_ci_changes.py`** (required checks) |
| `mcctl config` (get/set) | `mcctl/configedit.py`, `mcctl/yamledit.py` | `tests/test_mcctl_init.py`, `tests/test_client.py`, `tests/test_yamledit.py` |
| `mcctl migrate`, `mcctl retire-old` | `mcctl/migrate.py`, `mcctl/serverops.py` | **`tests/test_mcctl_p12.py`**, `tests/e2e/migrate_check.py` |
| `mcctl upgrade`, `mcctl lock` (update), `mcctl schema-migrate` | `mcctl/upgrade.py`, `mcctl/migrations.py`, `mcctl/wfsecrets.py` | **`tests/test_mcctl_p12.py`**, `tests/test_wfsecrets.py` |
| `mcctl secrets` (rotate, dashlane, show-restic) | `mcctl/rotate.py`, `mcctl/integrations.py`, `mcctl/dashlane.py` | **`tests/test_mcctl_p12.py`** (rotate), `tests/test_dashlane.py` |
| `mcctl players` | `mcctl/players.py`, `mcctl/providers/mojang.py`, `mcctl/publish.py` (v1.3 PR route); `kick`/`ban`/`pardon`: `mcctl/actions.py` | **`tests/test_mcctl_p12.py`**, `tests/test_mcctl_actions.py`, `tests/test_publish.py` |
| `mcctl publish` (v1.3: preview, PR / main / deploy, revert) | `mcctl/publish.py` (+ `mcctl/render.py`, `mcctl/upgrade.py` for the pinned `apply`/`deploy`) | **`tests/test_publish.py`** |
| `mcctl restore` | `mcctl/restorecmd.py` → `scripts/restore.py` on the box | **`tests/test_mcctl_p12.py`** |
| `mcctl discord` (sync) | `mcctl/integrations.py`, `mcctl/providers/discord.py` | `tests/test_discord.py` |
| `mcctl update-prs` (CI) | `mcctl/updates.py` | `tests/test_updates.py` |
| `mcctl expiry-check` (CI) | `mcctl/expirycheck.py`, `mcctl/providers/rdap.py` | **`tests/test_updates.py`** |
| `mcctl panel-pr-check` (CI) | `mcctl/panelpr.py` | `tests/test_panelpr.py` |
| `mcctl pr-gate` (CI) | `mcctl/gate.py`, `scripts/verify_lock_update.py` | `tests/test_gate.py` |
| `mcctl client-publish` (CI) | `mcctl/clientpublish.py` | `tests/test_client.py` |

Docs: [reference: mcctl commands](reference.md#mcctl-commands), [operations: mcctl commands](operations.md#mcctl-commands).

## Feature map

| Concern | Main code | Tests | Docs | CLAUDE.md rule |
|---|---|---|---|---|
| server.yml schema and rendering | `mcctl/schema.py`, `mcctl/render.py`, `mcctl/configrepo.py`, `schema/server.schema.json`, `ansible/group_vars/all/main.yml` (product constants) | `tests/test_mcctl_schema.py`, `tests/test_mcctl_render.py` | product-v1 §2 | Repo conventions: "server.yml fields are defined once", "Shared Ansible variables" |
| Config repo skeleton | `template/`, `examples/config-repo/` (generated), `examples/full/server.yml` (every feature on; the tests' reference deployment), `scripts/sync_example.py` | `tests/test_configrepo.py`, `tests/test_workflows.py` | [ci: Keeping a config repo's minutes low](ci.md#keeping-a-config-repos-minutes-low) | "This repo is the product" |
| Secrets chain | `mcctl/secrets.py` (the one table) → `terraform/github.tf` (Actions secrets) → `template/.github/workflows/server.yml` (passes them) → `.github/workflows/server.yml` (env `MC_SECRET_*`) → `ansible/group_vars/all/secrets.yml` (`mc_secret_*`); `mcctl/wfsecrets.py` (doctor/upgrade add secrets a config repo's workflow is missing) | `tests/test_consistency.py`, `tests/test_wfsecrets.py`, `tests/test_environments.py` | [reference: Secrets inventory](reference.md#secrets-inventory) | "Every secret Ansible needs is a Terraform output" |
| Terraform | `terraform/` (`terraform/variables.tf`, `terraform/outputs.tf`, `terraform/versions.tf`, `terraform/cloudflare.tf`, `terraform/tailscale.tf`, `terraform/github.tf`), `mcctl/tf.py` | `tests/test_mcctl_tf.py`, `tests/test_tf_drift.py` | [reference: Running Terraform](reference.md#running-terraform) | "Terraform lives in `terraform/`" |
| Ansible deploy | `ansible/site.yml`, `ansible/roles/`, `ansible/requirements.yml`, `mcctl/deploy.py` | `tests/test_playbooks.py`, `tests/test_mcctl_deploy.py`, `tests/e2e/deploy_check.py` | [operations](operations.md) | "Idempotency" |
| Server config overlays | `server-config/` (`*.j2`), `ansible/library/mc_config.py`, `scripts/config_keys.py`, `server-config/keys/` | `tests/test_overlays.py`, `tests/test_mc_config.py`, `tests/render_overlays.yml`, `tests/test_log4j2.py` | product-v1 §8 | "Server configs are overlays", "Config keys" |
| Performance | `server-config/*.j2`, `ansible/roles/minecraft/templates/minecraft.service.j2`, `ansible/library/mc_noatime.py`, `scripts/bench_gc.py`, `scripts/bench_profile.py`, `scripts/bench_telemetry.py`, `scripts/bench_view.py`, `plugin/tickprobe/` | `tests/test_performance.py`, `tests/test_mc_noatime.py`, `tests/e2e/perf_check.py`, `tests/e2e/p16.yml`, `tests/e2e/noatime_check.py` | [operations: Performance profile](operations.md#performance-profile), [Performance tuning](operations.md#performance-tuning), [perf-baseline.md](perf-baseline.md) | "Config keys" |
| Locks and updates | `scripts/update_lock.py`, `scripts/lockfile.py`, `scripts/fetch_server_jar.py`, `scripts/fetch_plugins.py`, `scripts/boot_test.py`, `mcctl/updates.py`, `mcctl/upgrade.py`, `.github/workflows/update-lock.yml` | `tests/test_lock_tools.py`, `tests/test_updates.py`, `tests/test_gate.py` | [reference: Bumping versions](reference.md#bumping-versions) | "Pin everything" |
| GitHub Apps, gate | `mcctl/ghapp.py`, `mcctl/gate.py`, `mcctl/panelpr.py`, `mcctl/productsetup.py` | `tests/test_mcctl_ghapp.py`, `tests/test_gate.py`, `tests/test_panelpr.py` | [SECURITY: The panel's GitHub App](../SECURITY.md#the-panels-github-app) | "GitHub Apps and rulesets" |
| Watchdog Worker | `watchdog/worker.js` (one file: watchdog, ingest, panel API, Discord, client page), `terraform/watchdog.tf`, `terraform/monitoring.tf`, `mcctl/providers/watchdog.py` | `watchdog/test/worker.test.mjs`, `watchdog/test/monitoring.test.mjs`, `tests/test_watchdog.py` (`NODE_TEST`: the node command, kept equal to the one in `.github/workflows/ci.yml`) | [reference: Architecture](reference.md#architecture) | "The watchdog Worker" |
| Panel | `watchdog/panel/index.html`, `watchdog/panel/panel.js`, `watchdog/panel/panel.css`, `watchdog/panel/confirm.js` (the confirmation host for destructive actions), `terraform/panel.tf`, `scripts/mc_panel.py` (actions on the box), `ansible/roles/players/` | `tests/test_panel.py`, `tests/test_panel_actions.py`, `watchdog/test/panel.test.mjs`, `watchdog/test/actions.test.mjs`, `watchdog/test/ui.smoke.mjs` | [operations: The panel](operations.md#the-panel), [SECURITY: Panel actions](../SECURITY.md#panel-actions-p10-what-the-panel-can-change) | "The watchdog Worker", "Panel actions" |
| mc-agent | `scripts/mc_agent.py`, `ansible/roles/agent/` | `tests/test_agent.py`, `tests/e2e/agent_check.py` | product-v1 §5a | "mc-agent" |
| World library (v1.2) | `scripts/worlds.py` (on the box: presets, archives, the switch, the folder swap, server.yml's world section), `scripts/world_info.py` (level.dat), `ansible/roles/backups/templates/mc-world.service.j2`, `ansible/roles/backups/templates/mc-world-apply.service.j2`, `ansible/roles/minecraft/tasks/worlds.yml` (deploys honour the live world), `scripts/mc_agent.py` (`world_*`), `mcctl/panelpr.py` (world PRs), `watchdog/panel/panel.js` (Worlds tab) | `tests/test_worlds.py`, `watchdog/test/actions.test.mjs`, `watchdog/test/ui.smoke.mjs`, `tests/e2e/agent_check.py` | [operations: World library](operations.md#world-library), [SECURITY: The world library](../SECURITY.md#the-world-library-v12) | "Panel actions", "Privilege separation" |
| Seed catalogue (v1.2) | `worlds/seeds.yml` (researched seeds, sources, claims), `scripts/seed_check.py` (boots the pinned jar, `/locate` over RCON; reuses `scripts/boot_test.py`), `watchdog/panel/seeds.json` (generated: the verified catalogue the panel serves), `watchdog/panel/panel.js` (New world dialog: "Start from") | `tests/test_seeds.py`, `watchdog/test/panel.test.mjs`, `watchdog/test/ui.smoke.mjs`, ci.yml job `seeds` | [operations: World library](operations.md#world-library) | "Pin everything" |
| Backups and restore | `scripts/backup.py`, `scripts/restore.py` (on the box), `scripts/restic_rekey.py`, `ansible/roles/backups/`, `mcctl/restorecmd.py` (CLI), `mcctl/serverops.py` | `tests/test_backup.py`, `tests/test_restore.py`, `tests/test_mcctl_p12.py` | [operations: Backups and restore drills](operations.md#backups-and-restore-drills) | "Privilege separation" (`mc-restore-apply.service`) |
| Maintenance window (v1.3: restarts only inside `maintenance.window`, deferred otherwise) | `mcctl/schema.py` (`Maintenance`, `restart_deferred`), `ansible/site.yml` (the decision), `ansible/roles/minecraft/tasks/defer.yml`, `ansible/roles/minecraft/tasks/restart.yml`, `ansible/library/mc_config.py` (`stage`), `scripts/restart_window.py`, `ansible/roles/minecraft/templates/mc-restart-window.*.j2`, `mcctl/publish.py` (preview) | `tests/test_restart_window.py`, `tests/test_publish.py`, `tests/test_hardening.py`, `tests/e2e/restart_window_check.py` | [operations: Maintenance window](operations.md#maintenance-window-v13) | "Idempotency", hardening |
| Health, self-heal, alerts | `scripts/health.py`, `scripts/alerts.py`, `scripts/notify.py`, `scripts/admin.py`, `scripts/reboot_gate.py`, `scripts/backup_check.py` (the weekly backup test, `mc-backup-check.timer`), `scripts/ci_alert.py` (CI job failures), `mcctl/alerttext.py`, `ansible/roles/monitoring/`, `ansible/roles/autoreboot/` | `tests/test_health.py`, `tests/test_notify.py`, `tests/test_admin.py`, `tests/test_reboot_gate.py`, `tests/test_backup_check.py`, `tests/test_alert_texts.py` (every alert's text, `tests/fixtures/alerts/`), `tests/e2e/reboot_check.py` | [operations: Alerts](operations.md#alerts), [Monitoring](operations.md#monitoring) | Server-side scripts are stdlib-only |
| Hardening, firewall, tailnet | `ansible/roles/hardening/`, `ansible/roles/firewall/templates/nftables.conf.j2`, `ansible/roles/base/templates/50-mc-ops.rules.j2`, `ansible/roles/tailscale/`, `terraform/tailscale.tf`, `terraform/tailscale-policy.json.tftpl` | `tests/test_hardening.py`, `tests/test_tailscale_policy.py`, `tests/e2e/hardening_check.py`, `tests/e2e/firewall_check.py`, `tests/e2e/firewall.yml` | [SECURITY.md](../SECURITY.md) | "Hardening", "Privilege separation" |
| Player-experience telemetry | `plugin/mc-telemetry/`, `scripts/telemetry.py`, `ansible/roles/plugins/templates/mc-telemetry.yml.j2`, `mcctl/metrics.py` | `tests/test_telemetry.py`, `watchdog/test/experience.test.mjs`, `tests/test_mcctl_metrics.py`, `tests/e2e/telemetry_check.py` | [operations: The Experience tab](operations.md#the-experience-tab) | "mc-agent" (budget) |
| Branding | `plugin/mc-brand/build.yml`, `scripts/brand_art.py`, `mcctl/brandpack.py`, `scripts/build_plugin.py`, `ansible/roles/plugins/templates/mc-brand.yml.j2` | `tests/test_brand.py`, `tests/e2e/brand_check.py` | [operations: Branding](operations.md#branding) | "Pin everything" (plugin hashes) |
| Wayfinding (v1.3: compass, `/map`, `/help`, client-mod precedence) | `plugin/mc-brand/src/main/java/io/github/edvardk/mcbrand/` (Wayfinding.java: the pure logic; Compass.java, PocketMap.java, Help.java), `plugin/mc-brand/src/test/java/io/github/edvardk/mcbrand/WayfindingFixtures.java` → `plugin/mc-brand/src/test/wayfinding-fixtures.json` (`scripts/build_plugin.py`) | `tests/test_wayfinding.py`, `tests/e2e/brand_check.py` | [features](features.md#finding-friends-without-mods-the-compass-map-and-help), [operations: Branding](operations.md#branding) | "Pin everything" (plugin hashes) |
| Client modpack (v1.1) | `client/pack.yml`, `scripts/client_pack.py` (writes the config repo's client lock), `scripts/update_lock.py`, `mcctl/clientpack.py` (builds the `.mrpack`), `mcctl/clientpublish.py`, `mcctl/configedit.py` (turning it on), `terraform/client.tf`, `watchdog/panel/client/`, `plugin/mc-brand/src/main/java/io/github/edvardk/mcbrand/Modpack.java` (`/modpack`) | `tests/test_client.py`, `watchdog/test/client.test.mjs` | client-v1.1.md, [operations: The client modpack and /view](operations.md#the-client-modpack-and-view) | – |
| Smallworld client mod (v1.2) | `client/mod/` (Fabric, client side), `client/mod/build.yml`, `scripts/build_client_mod.py` (reproducible build), `client/mod/WIRE.md` (the `smallworld:*` channels), `plugin/mc-brand/src/main/java/io/github/edvardk/mcbrand/Smallworld.java` | `tests/test_client_mod.py`, `tests/test_social.py` (server side: mc-brand, mc-telemetry, the Worker's FPS diagnosis), ci.yml job `plugin` | client-v1.2.md | "Pin everything" |
| Docs screenshots | `scripts/screenshots.py` (one entry point: `watchdog/test/ui.smoke.mjs` and `watchdog/test/client-smoke.mjs` for the panel and the client setup page, `scripts/brand_art.py`, `scripts/setup_screenshot.py`) → `docs/images/` | `tests/test_panel.py`, `tests/test_brand.py` | [features.md](features.md) | – |
| Discord | `mcctl/integrations.py`, `mcctl/providers/discord.py`, Worker `/discord/interactions` | `tests/test_discord.py`, `watchdog/test/discord.test.mjs` | [operations: Discord](operations.md#discord) | – |
| Dashlane | `mcctl/dashlane.py`, `mcctl/envfile.py` | `tests/test_dashlane.py` | [setup: Dashlane](setup.md#dashlane-optional) | "No secrets in the repo" |
| CI | `.github/workflows/ci.yml`, `.github/workflows/e2e-mock.yml`, `.github/workflows/dependabot-automerge.yml`, `scripts/ci_changes.py`, `scripts/deploy_record.py` (a config repo's deploy skip), `tests/conftest.py` (`CONTROLLER_TESTS`, `posix_only`), `tests/e2e/` | `tests/test_ci_changes.py`, `tests/test_workflows.py`, `tests/test_ci_oidc.py`, `tests/test_deploy_record.py` | [ci.md](ci.md) | Lint locally before committing |
| Release | `.github/workflows/release.yml`, `scripts/release.py`, `CHANGELOG.md`, `pyproject.toml`, `mcctl/__init__.py` | `tests/test_release.py` | [reference: Releasing](reference.md#releasing) | "Versioning" |
| Publishing (the fresh public repository, anonymity gate; after publishing the hooks and the `publication` check) | `scripts/publish_fresh.py`, `publish-exclude`, `scripts/guard.py`, `.githooks/` | `tests/test_publish_fresh.py`, `tests/test_guard.py`, `tests/test_publishable.py`, `tests/fixtures/publishable-known.txt` (shrinking list of files still to clean) | [publishing.md](publishing.md) | "No secrets in the repo" |
| Rust workspace (v1.4: `smallworld-rcon`, the drop-in for `scripts/mc_rcon.py`'s command line) | `rust/smallworld-rcon/src/main.rs` (`args.rs`, `ops.rs`), `rust/smallworld-core/src/` (`rcon.rs`, `mcrcon.rs`, `properties.rs`, `pystr.rs`, `error.rs`, `pyjson.rs`, `fsx.rs`, `flock.rs`, `audit.rs`, `paths.rs`), `rust/smallworld-agent/` (the port of `mc_agent.py`; `PORTING.md`), `rust/allowed-crates.txt`, `rust/deny.toml`, `rust/rust-toolchain.toml`, `rust/Cargo.lock`, `rust/dist/` (committed binaries), `scripts/build_rust.py` (reproducible build + check), `ansible/roles/base/tasks/main.yml` (install), `ansible/roles/minecraft/templates/minecraft.service.j2` (`ExecStop` + fallback) | `cargo test` in `rust/`, `tests/test_rust_rcon.py` (differential vs `scripts/mc_rcon.py`, the `ExecStop` fallback), `tests/test_rust_agent_cli.py` (differential vs `mc_agent.py cli`), `tests/test_consistency.py` (pins), `tests/e2e/deploy_check.py`, ci.yml job `rust` | [rust/smallworld-rcon.md](rust/smallworld-rcon.md) (walkthrough for Python programmers) | "Rust workspace" |
| Python dependencies | `requirements/` (`.in` + hashed `.txt`) | `tests/test_consistency.py` | `requirements/validate.in` | "Python dependencies" |

## Look-alikes

| These | Differ in |
|---|---|
| `mcctl/clientpack.py` vs `scripts/client_pack.py` | mcctl builds the `.mrpack` files and (v1.2) the official-launcher zips from a lock; the script resolves and verifies upstream mods into the lock |
| `mcctl/restorecmd.py` vs `scripts/restore.py` | the laptop CLI (`mcctl restore`) vs the stdlib script it runs on the server |
| `mcctl/migrate.py` vs `mcctl/migrations.py` | moving the server to a new machine vs server.yml schema migrations between releases |
| `mcctl/secrets.py` vs `mcctl/wfsecrets.py` vs `ansible/group_vars/all/secrets.yml` | the table of Terraform-output secrets vs which secrets a config repo's workflow must pass vs the Ansible variables reading `MC_SECRET_*` |
| `mcctl/expiry.py` vs `mcctl/expirycheck.py` | the expiry.yml file vs the weekly CI check that reads it |
| `scripts/verify_lock_update.py` vs `mcctl/gate.py` | the lock-update rule vs the `server / gate` check that calls it |
| `scripts/mc_rcon.py` vs `rust/smallworld-rcon/` | the Python module (imported by about ten scripts) and its command line vs the Rust binary that replaces only the command line (v1.4: `ExecStop`, countdown, world border, game rules, `mc`) |
| `scripts/mc_agent.py` vs `scripts/mc_panel.py` | the outbound agent vs the panel actions it executes |
| `mcctl/actions.py` vs `scripts/mc_agent.py` `cli_main` | mcctl's side of the v1.3 actions (pre-checks, typed confirmations, SSH) vs the server's entry point that runs the agent's Executor for them |

## Tests in unexpected places

- `tests/test_ci_changes.py`: also `mcctl/productsetup.py`'s required checks.
- `tests/test_updates.py`: also `mcctl/expirycheck.py` (`mcctl expiry-check`).
- `tests/test_mcctl_p12.py`: `migrate`, `retire-old`, `upgrade`, `lock update`, `schema-migrate`, `secrets rotate`, `players`, `restore`.
- `tests/test_agent_cli.py`: `mc_agent.py cli` (mcctl's actions on the box) and the cross-process job lock of `mc_panel.Jobs`.
- `tests/test_mcctl_core.py`: the tailnet wrappers (`status`, `rcon`, `ssh`, `logs`, `backup`), expiry.yml, `mcctl/state.py`.
- `tests/test_watchdog.py`: runs the Worker's node tests (`NODE_TEST`, the same command as `.github/workflows/ci.yml`).
