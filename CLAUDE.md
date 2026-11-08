# CLAUDE.md — rules for agents working in this repo

Read `docs/reference.md` for the architecture. Work one phase at a time; at the end of each phase run its
acceptance checks, commit as `phase-N: <summary>` (N = the product phase Pn: glossary in `docs/map.md`), then stop
and report to the maintainer.

Where things are: `docs/map.md` (feature map + phase glossary).

## Ground rules

- **Verify, don't assume.** Paper/Purpur config keys, Terraform provider schemas and API endpoints change between versions.
  - Check every config key against the generated default config for the pinned version.
  - Check every Terraform resource against the provider schema for the pinned provider version
    (`terraform providers schema -json` is the source of truth).
  - If something no longer exists, find the current equivalent or drop it. **Never invent keys or resources.**
- **No secrets in the repo, ever.** Terraform state is the only secret store; CI gets them as GitHub Actions
  secrets, Ansible as `MC_SECRET_*` environment variables. Never echo secrets in logs or put them on a command line.
  Never ask the maintainer to paste secrets into the chat; say which secret to create, and where.
- **Python** for all helper scripts (`ruff`-clean, type-hinted). Shell only for trivial glue.
- **Pin everything:** Terraform and provider versions (`.terraform.lock.hcl` is committed), Ansible collections,
  server build, plugin versions and hashes, GitHub Actions by commit SHA.
- **Stop and ask the maintainer before:**
  - any `terraform apply`,
  - any action that touches the live world,
  - choosing the Minecraft version,
  - anything that costs money.
- **Idempotency:** running Ansible twice must report zero changes the second time.
- Prefer boring, well-documented tools over clever ones.

## Repo conventions

- **This repo is the product: code only, no deployment values.** A deployment is a config repo (skeleton in
  `template/`, CI fixture `examples/config-repo/`, every-feature reference `examples/full/server.yml`). After changing `template/`
  run `python scripts/sync_example.py`. Never put a deployment value (host, zone, login, bucket, tunable) into
  `terraform/`, `ansible/` or workflows: add a `server.yml` field and render it.
- Terraform lives in `terraform/`: no tfvars, no backend values. Inputs come from `mcctl render`
  (`-var-file=<config>/.mcctl/rendered/terraform/terraform.tfvars.json`, `-backend-config=.../state.tfbackend`),
  secrets from `TF_VAR_*` in the config repo's `.env` (git-ignored).
- Every secret Ansible needs is a Terraform output, listed once in `mcctl/secrets.py`: Actions secret `NAME` of the
  config repo (`terraform/github.tf`) → passed explicitly by the config repo's workflow (`template/.github/workflows/server.yml`)
  → env `MC_SECRET_NAME` (reusable `.github/workflows/server.yml`, or `python -m mcctl deploy -C <config>` locally) →
  `mc_secret_name` (`ansible/group_vars/all/secrets.yml`). Tasks that write one use `no_log: true`.
  `tests/test_consistency.py` checks all of these; add a secret in every place or the tests fail.
- Server configs are **overlays** (`server-config/*.j2`, only managed keys) applied by `ansible/library/mc_config.py`,
  which compares parsed values. Never switch to full-file templates: the server rewrites its configs and every
  deploy would restart it.
- Config keys: after changing `minecraft_version` or plugins, regenerate `server-config/keys/<version>/` with
  `scripts/config_keys.py` (static analysis of the pinned jar, never runs it); `tests/test_overlays.py` enforces it.
  The one exception, a **build-time registry dump**: `scripts/map_palette.py` (v1.4 world map) runs
  `scripts/map_palette/Dump.java` on the pinned JDK against only the pinned, hash-checked jars, without network,
  to bootstrap the block registry (never the server, never a world) and write
  `server-config/keys/<version>/map-palette.tsv` + `map-colors.tsv`; re-run `dump` after a version bump (CI runs
  `check`, `tests/test_map_reference.py` checks they exist).
- Server-side scripts (`mc_rcon`, `notify`, `backup`, `restore`, `health`, `admin`, `mc_agent`, `mc_panel`) are
  stdlib-only, Python >= 3.11.
  The controller (`mcctl`) needs Python >= 3.12 (`mcctl.MIN_PYTHON` = `requires-python`; pinned ansible-core).
- Rust workspace (v1.4, `rust/`, `docs/rust/smallworld-rcon.md`, `docs/rust/smallworld-map.md`): the toolchain is
  pinned exactly in `rust/rust-toolchain.toml` (a stable release at least two weeks old); `Cargo.lock` is committed
  and builds run `--locked`. `smallworld-rcon`, `smallworld-map` and `smallworld-core` (without features) stay std
  only with `unsafe` forbidden (`tests/test_rust_rcon.py`). `smallworld-agent` (the port of mc_agent.py,
  `rust/smallworld-agent/PORTING.md`) uses only the crates pinned exactly in `rust/Cargo.toml`
  `[workspace.dependencies]` (each at least two weeks old), listed in `rust/allowed-crates.txt` (checked against
  `Cargo.lock` by `tests/test_consistency.py`) and passing `cargo deny check` (`rust/deny.toml`); a new or bumped
  crate is a maintainer decision; `unsafe` only in its `sys.rs`. The binaries in `rust/dist/` are committed and
  pinned (`mc_smallworld_rcon_sha256`, `mc_smallworld_map_sha256`, `mc_smallworld_agent_sha256`; every crate in
  `build_rust.py`'s `BINARIES`): after changing Rust code or the toolchain run `python scripts/build_rust.py build`
  (needs clang 18.1.3 and llvm-ar-18 for ring), update the pins, and make sure `python scripts/build_rust.py check`,
  `cargo fmt --check`, `cargo clippy --all-targets -- -D warnings` and `cargo test` pass (ci.yml job `rust`).
  `smallworld-rcon` must stay identical to `mc_rcon.py`'s command line, and `smallworld-agent cli` to
  `mc_agent.py cli` (the differential tests): change both or neither.
- Cross-file pins (restic, Tailscale, Temurin vs `java_major`, secrets used by workflows) are checked by
  `tests/test_consistency.py`; extend it when adding a pin that lives in two places.
- Shared Ansible variables use the `mc_` prefix. Product constants (pins, paths) live in
  `ansible/group_vars/all/main.yml`; deployment values reach Ansible only through the rendered inventory's
  group_vars (`mcctl/render.py`). `tests/test_mcctl_render.py` keeps the two sets disjoint and exact.
- The reusable workflow (`.github/workflows/server.yml`) is the product's public interface: config repos pin it by
  SHA and pass secrets explicitly. Keep its inputs/secrets backwards compatible within a major version.
- Versioning: semver tags `vX.Y.Z`; `pyproject.toml`, `mcctl/__init__.py` and `CHANGELOG.md` must agree
  (`scripts/release.py`: `bump X.Y.Z` on a branch, `tag` on the merged `origin/main`). Add user-visible changes
  to `## [Unreleased]`.
- Python dependencies: direct pins in `requirements/{validate,deploy,dev}.in`, fully resolved with hashes in
  `requirements/*.txt` (pip-tools `pip-compile --generate-hashes`; command in `requirements/validate.in`), always
  installed with `--require-hashes --no-deps`. Recompile all three after editing a `.in`; `tests/test_consistency.py`
  checks they agree. Only the job that joins the tailnet may request `id-token: write`.
- mcctl talks to provider APIs only through `mcctl/http.py` transports and to tools through injectable runners, so
  tests replay recorded responses (`tests/fixtures/http/`, `conftest.Replay`) and fake binaries. Tests never call a
  real provider API or run a real `terraform apply`. Credential values never go into argv, messages, logs,
  `expiry.yml` or `.mcctl/state.json`; `apply` always takes a typed confirmation (no `--yes`).
- `server.yml` fields are defined once, in `mcctl/schema.py` (dataclasses; `mcctl render` maps them to
  Terraform/Ansible variables). After changing it run `python -m mcctl schema` to regenerate
  `schema/server.schema.json`.
- `mcctl init` (`mcctl/init.py`): every step is idempotent, records itself in `.mcctl/state.json` and takes
  all outside effects through `init.Deps` (`mcctl/mock.py` fakes them for `--mock`, the tests and
  `.github/workflows/e2e-mock.yml`). Credentials only via `ui.secret` (getpass / environment), validated live,
  written with `envfile.write` (0600); never printed, on argv, in state.json or in git. New questions need a
  key in `tests/e2e/answers.yml` (never a secret).
- `server.yml` is hand-edited: change it with `mcctl/yamledit.py` (comment-preserving) and check values with
  `schema.field_errors` first; never re-dump it with PyYAML.
- Hardening (P7, `SECURITY.md`): every systemd unit the product installs includes `mc_systemd_sandbox`
  (`ansible/group_vars/all/main.yml`; non-JIT units add `MemoryDenyWriteExecute=true`) and must stay within the
  `systemd-analyze security` gate (≤ 2.5, minecraft ≤ 4.5: `tests/test_hardening.py` offline, e2e live). New
  hardening goes in the `hardening` role and is checked live in `tests/e2e/hardening_check.py`; firewall changes
  must pass `tests/e2e/firewall_check.py`. Tailnet policy: `terraform/tailscale-policy.json.tftpl` (its `tests`
  run in `tests/test_tailscale_policy.py`). gitleaks is pinned (version + sha256) in `ci.yml` and the reusable
  `server.yml` alike. Update SECURITY.md with any change to the attack surface.
- Privilege separation (P7 review): only the JVM and `mc-restore-apply.service` run as `minecraft`. Anything that
  holds a credential runs as `mc_ops_user` (`mcops`, group `minecraft`), with systemd actions only through
  `ansible/roles/base/templates/50-mc-ops.rules.j2`. Never run a credential-holding process as `minecraft`;
  `tests/e2e/hardening_check.py` proves the separation live.
- mc-agent (P8, `scripts/mc_agent.py`, role `agent`): outbound only, runs as `mcops`; a new command needs the
  agent's `ACTIONS` + executor, the Worker's `COMMAND_ACTIONS`, a polkit verb if it touches systemd, and tests on
  both sides. Keep `schema.budget` >= `mc_agent.daily_budget` (`tests/test_agent.py`).
- The watchdog Worker (`watchdog/worker.js`) stays one dependency-free ES module with no build step; the panel UI
  (`watchdog/panel/`: `index.html`, `panel.css`, `panel.js` (one ES module, no dependencies), self-hosted fonts) is
  uploaded as Workers static assets and served only through the Worker (`run_worker_first`, Access JWT check,
  `ASSET_PATHS`). No inline scripts or styles, no third-party URLs (the CSP is `'self'` only): `tests/test_panel.py`.
  Worker tests run with node's built-in runner (the exact command is `NODE_TEST` in `tests/test_watchdog.py`,
  which keeps it equal to `ci.yml`'s; Node >= 22.13, the panel tests use `node:sqlite` as D1; also via
  `tests/test_watchdog.py`), never against the network. The UI smoke test (`watchdog/test/ui.smoke.mjs`:
  Playwright, desktop + phone, no console errors, no horizontal scroll, P10's action dialogs) runs locally through
  `tests/test_panel.py` (skipped without Playwright).
- Panel actions (P10): a new action needs the Worker's `actionArgs` (shape; destructive ones go in `DESTRUCTIVE`:
  accepted only on the confirmation host, `confirm.js`), the agent's
  `ACTIONS` + `do_<action>` (its own checks; both lists must match, `tests/test_agent.py`), a confirmation dialog in
  `panel.js`, and, if it needs privileges, a fixed systemd unit allowed by `50-mc-ops.rules` (never root, never a
  generic `systemctl`). Long actions run as `mc_panel.Jobs` (countdown, one at a time, job reports). Whitelist
  changes go live and to Git (`pending.json` → PR → the reusable workflow's `panel-merge`); deploys honour pending
  entries (`players` role). Prove each action end to end in `tests/e2e/agent_check.py`.
- GitHub Apps and rulesets (P10 review, SECURITY.md "The panel's GitHub App"): no GitHub credential on the server
  or in the deploy job. The panel App's key lives only in the Worker (it mints narrowed 1 h installation tokens);
  the updater App (Workflows write) only in the scheduled update job's step env. Whatever may merge without the
  admin must pass `mcctl pr-gate` (`mcctl/gate.py`, the required `server / gate` check in `terraform/github.tf`),
  decided by the diff, never by branch name or author; App permissions are exactly `mcctl/ghapp.py`'s.
- Anonymity (docs/publishing.md "After publishing"): install the hooks once per clone (`python3 scripts/guard.py
  install`) and never bypass them (`--no-verify`, `core.hooksPath` changes): they refuse denylisted text,
  personal emails, non-noreply commit identities and `publish-exclude` paths. Commit messages carry no session
  links (the commit-msg hook refuses them). The `publication` CI check runs the same checks on every PR.
- Lint locally before committing:
  `ruff check . && ruff format --check . && yamllint --strict . && pytest -q && (cd ansible && ansible-lint) && terraform -chdir=terraform fmt -check -recursive`
  (plus `terraform -chdir=terraform init -backend=false && terraform -chdir=terraform validate`; CI also runs actionlint;
  after Rust changes the cargo checks and `python scripts/build_rust.py check`).
