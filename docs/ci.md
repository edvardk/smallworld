# CI and its Actions minutes

The product repo is public: GitHub's standard runners cost it no Actions minutes, so its CI is thorough (e2e on
every pull request and nightly, the controller on Linux, macOS and Windows). A deployment's **config repo** is
usually private, and its minutes come out of its owner's included minutes (2000 a month on GitHub Free, 3000 on
Pro; Linux runners count 1:1, **every job is rounded up to a whole minute**, and a skipped job costs nothing). The
reusable workflow it runs is built to keep those near zero: see
[Keeping a config repo's minutes low](#keeping-a-config-repos-minutes-low).

## What runs when

| Event | `ci.yml` | `e2e-mock.yml` |
|---|---|---|
| Pull request, docs only (`docs/**` except the brand previews, root `*.md`, `LICENSE`) | `lint` (ruff, yamllint, the tests that read the docs), `gitleaks`, `publication`, `config-repo / validate` | `e2e` essential half |
| Pull request, code | `lint` (everything), `gitleaks`, `publication`, `config-repo / validate`, `controller` (3 OS x 3 Pythons); `plugin` / `rust` / `seeds` if their inputs changed, `actionlint` if a workflow changed | `e2e` essential half; the heavy half too if the change touches what those checks exercise |
| Nightly, 22:07 / 22:37 UTC on `main` | everything | the whole `e2e` |
| Monthly, the 1st, 23:43 UTC | | the four benches, if the month's commits touched what they measure |
| `release.yml` (tag push) | everything, including `controller` | |
| `workflow_dispatch` | everything, including `controller` | `scope`: `full` (default) / `essential` / `none`; `bench: true` adds the benches |
| Push to `main` | nothing (the pull request already tested the merge commit; the nightly run catches two PRs that break only together) | nothing |

The path groups live in one place, `scripts/ci_changes.py` (`code`, `workflows`, `plugin`, `rust`, `seeds`, `e2e`, `e2e_full`),
tested by `tests/test_ci_changes.py`. When the change set can't be known (a dispatch, a tag, a git error) the
answer is "everything changed".

- **The essential e2e half:** `mcctl init` twice and the stale-pin move, validate + render, terraform validate,
  `site.yml` twice (the second must report `changed=0`), the status ping, the hardening checks, the agent and
  panel actions, the reboot gate, the deploy checks, the firewall ruleset, the P12 dry runs.
- **The heavy half:** experience telemetry (plugin, agent, real Worker code), branding, migrate container ->
  container, the P16 knobs. It runs on a pull request that touches `plugin/`, `watchdog/`, `server-config/`,
  `tests/e2e/`, the lock files, `ansible/site.yml`, the `base`/`java`/`minecraft`/`plugins`/`backups` roles, or
  the migrate/restore/telemetry code, and in the nightly run.
- **Shards:** the checks run in parallel, each shard on its own runner with its own freshly deployed box (they
  can't share one: host-network ports, restarts, restores, world switches, paused timers). Job `e2e-shard`
  (static matrix, `fail-fast: false`):

  | Shard | Checks (after its own boot + first `site.yml`, ~3.5 min) | Runs | Wall time |
  |---|---|---|---|
  | `core` | `mcctl init` re-run and stale pin, the client switch, `terraform validate`, the second `site.yml` (`changed=0`), hardening, reboot gate, deploy checks, firewall, P12 dry runs | always | ~10 min |
  | `agent-a` | `agent_check.py --rounds a`: mc-agent's pushes and first commands, then the rounds rcon, client code, stop/start, whitelist, restart, plugin, restore guard, versions (v1.3: version file, Panel → Git self-test, Mojang lookup, ~20 s), cli (v1.3: mcctl's actions through `mc-agent cli`, pending.json shared with mc-agent, ~40 s) | always | ~10-11 min |
  | `agent-b` | `agent_check.py --rounds b`: mc-agent's pushes and first commands, then the rounds backup/restore, world library | always | ~10 min |
  | `telemetry-brand` | experience telemetry, then branding | heavy half | ~11 min |
  | `migrate-p16` | migrate container -> container, then the P16 knobs on the migrated box | heavy half | ~10 min |
  | `agent-a, rust`, `agent-b, rust`, `telemetry-brand, rust` | v1.4: the same shards with `agent.implementation: rust` (`mcctl config set` before the render): the Rust mc-agent (its telemetry in-process) and `mc-agent cli` through the same black-box rounds; `agent-a, rust` also scores the agent's units live (`hardening_check.py --agent`); `telemetry-brand, rust` runs the telemetry half only | like their Python shard | in parallel: no extra wall time |

  The times are a measured full run (v1.2.2: core 10, the one `agent` shard 16, telemetry-brand 11, migrate-p16
  9.5); v1.3 cut `agent` in two by its rounds. Each agent shard repeats the shared part (the agent's sandbox, the
  buffered and live metrics, the journal archive, the first poll's signed commands) and checks that every
  command it sent is in the audit journal; each round sets up its own preconditions (the world round seeds the
  mock GitHub repository itself; whitelist always brings stop/start, which seeds the profile cache) and prints
  its time (`round <name>: N s`, then a `timings:` line), so the split can be rebalanced from a run's log
  (`GROUPS` in `tests/e2e/agent_check.py`). Without `--rounds` the script runs every round, as one shard did
  before (for a local box).

  A heavy shard in an essential run skips every step (its boot too) and passes in seconds. Each check sets up
  its own preconditions, so none depends on another shard; the failure journal dump runs in every shard.
- **The benches** (`bench`, `bench-gc`, `bench-brand`, `bench-profile`, v1.1 `bench-view`) never run on pull requests. Dispatch
  `e2e-mock` with `scope: none` and `bench: true` when a change could move tick times (a plugin, the JVM flags,
  the performance profile).
- `lint` runs on Python 3.12 (`MIN_PYTHON`); `controller` runs on every OS and Python (below).
- `update-lock` dispatches `ci` and the whole `e2e` (`scope=full`) on its PR branch, as before.
- Dependabot opens at most one grouped PR per ecosystem and month (minor and patch updates; a major update gets its
  own PR and waits for a human). The auto-merge job only enables `--auto`, so the merge waits for every required
  check.

## Required checks

Main's ruleset (`mcctl product-setup`, `REQUIRED_CHECKS` in `mcctl/productsetup.py`) requires `lint`,
`gitleaks`, `publication` (the anonymity checks, [publishing](publishing.md#after-publishing-safeguards-on-every-commit)),
`actionlint`, `config-repo / validate` and `e2e`. A workflow skipped by a `paths:` filter never reports, so a
required check would wait forever. Neither workflow therefore has a path filter: a small
`changes` job decides, and

- `lint`, `gitleaks`, `publication` and `config-repo / validate` always run (`lint` cheaply on a docs-only change; the
  reusable-workflow call would report under another name if it were skipped);
- `actionlint` is skipped by its `if:` when no workflow changed (a skipped job reports success); `e2e` runs on
  every pull request. It is an aggregator (`needs: [changes, e2e-shard]`, no checkout): its one step fails
  unless `needs.e2e-shard.result` is `success` (the shards share its `if:`, so none is skipped while it runs), so the required name stays `e2e` however the shards
  are cut, and one failed shard fails it. It has the shards' own `if:` (`!cancelled()` plus
  `needs.changes.outputs.e2e != 'false'`), so it waits for all of them and is skipped only with them (scope
  `none`, the monthly benches);
- if `changes` itself fails, its outputs are empty, and every `!= 'false'` condition runs the job.

`tests/test_ci_changes.py` checks all three.

## The controller on every OS

`controller` (ci.yml) runs on `ubuntu-24.04`, `macos-15` and `windows-2025` x Python 3.12 (`MIN_PYTHON`), 3.13 and
3.14, on pull requests that touch code and nightly. Linux runs the whole suite. macOS and Windows run
`pytest -m "controller and not posix_only"`:

- `controller` is set by `tests/conftest.py` on the files in `CONTROLLER_TESTS`: the controller's pure logic
  (schema, render, the gate, the anonymity guard, update PRs, the pin move, workflow secrets, metrics, providers through recorded HTTP).
  The flows that drive ssh, terraform, Ansible or fake binaries (init, deploy, doctor, tf, p12, galaxy, ui) and
  the box's scripts (fcntl, pwd, grp) are left to Linux. On Windows the other test files aren't even collected.
- `posix_only` marks a test in those files that needs POSIX (a `#!/bin/sh` fake binary, a 0600 file mode,
  ansible-playbook). It is skipped on Windows. Mark a test rather than deleting it when a new cell fails.
- mcctl itself refuses native Windows (its preflight says to use WSL2); `mcctl --help` proves the CLI starts
  there. `PYTHONUTF8=1`: Windows' Python reads text as cp1252 otherwise.

Not a required check.

## The agent parity harness

`tests/agent_parity/` is the black-box bar for a second mc-agent implementation (the Rust port planned for
v1.4.0): it runs an implementation as a process and compares what it does with golden traces from the Python
agent. Part of the normal `pytest -q` on Linux (lint and the Linux `controller` cells), about 20 s: the scenarios
run in parallel threads.

- **Isolation without test hooks.** Each run is `unshare -rm` (user + mount namespace) through
  `tests/agent_parity/nsinit.py`: tmpfs over `/opt`, `/var/lib` and `/run` (their entries bound back), then the
  scenario's directories at `/opt/minecraft`, `/var/lib/{minecraft,mc-agent,mc-snapshots}`,
  `/run/{mc-verdicts,mc-telemetry}` and a harness socket at `/run/systemd/journal/socket` (the audit entries).
  No production code changed for it. Ubuntu 24.04 restricts unprivileged user namespaces through AppArmor, so
  ci.yml lifts that (`sysctl kernel.apparmor_restrict_unprivileged_userns=0`) and sets `AGENT_PARITY_REQUIRE=1`,
  which turns "namespaces unavailable" from a skip into a failure.
- **Fakes** (`fakes.py`, `fake_systemd.py`): the Worker's agent routes and its WebSocket (watchdog/worker.js's
  protocol), GitHub's contents/refs/pulls endpoints and the two Mojang lookups, Discord and the watchdog's
  `/maintenance` over TLS (a throw-away certificate via `SSL_CERT_FILE`), RCON, and `systemctl`/`journalctl`
  first on `PATH`, recording their argv.
- **Traces**: requests per fake, audit datagrams (byte-exact fields), RCON commands per connection, systemctl
  and journalctl calls, the files a run changed (content, mode), cli output and exit codes. Times, nonces and
  host metrics are normalised (`harness.norm`, `norm_sample`); a scenario marks a section it can't make
  deterministic as not compared (`s.skip`, e.g. which pull request an interleaved write lands in).
- **Running it**: `pytest tests/agent_parity` (= `--impl=python`); `--impl=rust --agent-bin=PATH` runs the same
  scenarios against a binary invoked as `BIN run` and `BIN cli LOGIN ACTION [ARGS_JSON]` (skipped without one);
  `--update-golden` rewrites `golden/` from the Python agent after an intended behaviour change. The
  `state_roundtrip` scenario always runs Python before and after the implementation under test.
- **Test vectors** for the pure functions (console allowlist, `_one_line`, the scrubber, `verify`,
  players.yml, `merge_sync`, tokens, alert texts, parsers, WebSocket framing, `json.dumps`, `round`, Unicode
  properties, constants): `python scripts/parity_vectors.py` writes `tests/agent_parity/vectors/*.json` for
  `cargo test`; `tests/agent_parity/test_vectors.py` fails when they are stale. Files marked
  `unicode_sensitive` are only compared on a Python with the Unicode version they were generated with.

## The telemetry parity harness

`tests/telemetry_parity/` is the same bar for the player-experience telemetry (`scripts/telemetry.py`, ported to
Rust in v1.4.0): it runs an implementation as `<bin> telemetry`, the telemetry alone in a process (Python:
`scripts/mc_agent.py telemetry`; both agents run the same code in-process, and no unit runs this entry point), and
compares what it does with golden traces from Python. Part of the normal `pytest -q` on Linux, about 55 s (the scenarios run in parallel threads; the
slowest wait for a 30 s segment flush).

- **Reuses the agent harness** (`tests/agent_parity`): the namespace box (`nsinit.py`; plus
  `/var/lib/mc-telemetry`), `FakeWorker` (scripted `/ingest/experience` replies with verdicts, 5xx and 429, and
  `/ingest/segment`), fake `systemctl` (several units: one block each, as `systemctl show` prints them) and
  `journalctl`, `AgentProc`, the `--impl` / `--agent-bin` / `--update-golden` options (`--update-golden` is
  refused with `--impl=rust`) and `AGENT_PARITY_REQUIRE=1`. The scenarios write the mc-telemetry plugin's
  snapshots into the box's `/run/mc-telemetry` (atomically, as `Writer.java` does).
- **Time control without a test hook.** A minute row closes when a snapshot of a later minute arrives, or at
  the first read loop after the minute is 75 s old; the snapshots carry fixed timestamps in January 2026, so
  rows are deterministic and close at the latest one read loop (5 s, `Config.read_s`, not configurable) after
  their snapshots appear. agent.env's own knobs are set to their minimums: `SEGMENT_S=30`, `LOG_BATCH_S=10`
  (the overhead journal round), `BACKFILL_PAUSE_S=0`. A scenario waits for an event (a request, a rewritten
  `online.json`: one per read loop) before its next input. What would take minutes of real time (the
  back-off ladder, the row give-up after `BACKFILL_ROW_GIVE_UP` server errors) is in the vectors instead: `Pacer`
  and `Backfill.run_once` decision traces with an injected clock. No production code changed for it.
- **Scenarios**: `minute_row` (snapshots -> row -> POST -> `verdicts.tsv`), `sessions`, `lag`, `overhead`,
  `segments` (flush, PUT, retention by `SEGMENT_DAYS` / `SEGMENT_CAP_MB`), `backfill_quiet` (only in quiet
  time; a player online stops it), `backfill_errors` (failed PUT, batch halving, back-off), `bad_snapshots`
  (`MAX_SNAPSHOT_BYTES`, the name pattern, an older timestamp, symlink, FIFO, junk fields, a plugin restart),
  `identity`, `online` (`online.json`), `state_roundtrip` (Python -> implementation -> Python: identity,
  backlog, segments) and `startup_errors`. `Messages` (player_message's rate limits) isn't reachable from the
  telemetry alone (player_message runs in the agent): vectors here, and the agent harness's `experience`
  scenario (the daemon with its in-process telemetry).
- **The Rust gate**: ci.yml's `rust` job runs every scenario against the committed release binary
  (`RUST_TELEMETRY_SCENARIOS`, kept equal to the goldens by `tests/test_telemetry.py`); `cargo test` replays the
  vectors (`rust/smallworld-agent/src/telemetry/vectors.rs`).
- **Traces**: every Worker request (status, auth, user agent, body; a row in full the first time, then
  `{"ref": id}` when an equal copy is backfilled or archived), the files the runs changed (mode; segments as
  decompressed NDJSON, JSON parsed and byte-exact), the fake systemctl/journalctl calls, stdout/stderr and exit
  codes. Normalised: `ovh.*.cpu_pct` (a counter delta over wall-clock time), the backlog's byte counts (gzip
  sizes aren't part of the contract), the `verdicts.tsv` header's time, a random installation id.
- **Vectors**: `python scripts/telemetry_vectors.py` writes `tests/telemetry_parity/vectors/*.json` (text,
  snapshot, minute, overhead, segments, backfill, verdicts, identity) for `cargo test`;
  `tests/telemetry_parity/test_telemetry_vectors.py` fails when they are stale. `python_raises` entries are
  inputs on which Python raises (reported, not a contract).
- **Rust in CI**: ci.yml runs it for Python only for now; the port adds its scenarios to the rust job (a list
  beside `RUST_PARITY_SCENARIOS`) with `pytest tests/telemetry_parity --impl=rust --agent-bin=PATH`.

## Guards against wasted minutes

- `concurrency` per workflow and ref: a new push to a pull request cancels the obsolete run. Scheduled and
  dispatched runs queue instead.
- `timeout-minutes` on every job (`tests/test_ci_changes.py`). Every e2e step has its own timeout too, so a hang
  fails within minutes instead of running into the job's limit.
- Caches: pip downloads (`setup-python`, keyed by `requirements/*.txt`); the pinned Ansible collections (keyed
  by `ansible/requirements.yml`, product CI only); Terraform providers (keyed by
  `terraform/.terraform.lock.hcl`; terraform still checks them against the lock); the plugin build's server jar
  and compile-only jars (keyed by `template/server.lock.yml` and `plugin/*/build.yml`; `build_plugin.py`
  re-verifies their sha256 on every use; the JDK is downloaded and checked each run). The e2e container
  downloads Purpur and Temurin inside the box, hash-checked by Ansible. That isn't cached, because it is part of
  what the test proves. Node has no packages and Playwright isn't installed in CI (the UI smoke test is local).

## Expected runner time (product repo)

No minutes are billed (public repo). For wall-clock planning: a docs-only PR ~11 minutes (the essential e2e:
its slowest shard), a code PR ~11-15 (e2e's shards and the controller matrix run in parallel, ~5-10 per cell),
the nightly run well under its former ~70 (e2e's heavy half now runs beside the rest), the benches ~110.
The e2e shards together use about as much runner time as the one job did, plus a boot and deploy (~3.5 min) per
extra shard; e2e's wall time is its slowest shard (~11 min; 16 with one `agent` shard in v1.2.2, ~23 essential /
~42 full as one job).

### An optional self-hosted runner for e2e

The e2e shards (job `e2e-shard`) can move to your own machine (faster, or for a private fork that pays for minutes): set a repository variable `E2E_RUNNER`
(Settings > Secrets and variables > Actions > Variables, e.g. `self-hosted`). The workflow uses it only for
trusted code: the schedule, `workflow_dispatch` and pull requests whose head branch is in the product repo
itself (`github.event.pull_request.head.repo.full_name == github.repository`). A pull request from a fork always
runs on `ubuntu-24.04`. A self-hosted runner executes the branch's code with `--privileged` Docker and `sudo` on
that machine, so on a public repo also turn on Settings > Actions > General > "Require approval for all external
contributors", and give push access only to people you would let run code there. The machine needs Docker,
passwordless `sudo` (`modprobe`), cgroup v2, Python 3.13 and the Terraform CLI that `setup-terraform` installs.
Use a throwaway VM, not your desktop. Delete the variable to go back to GitHub's runners.

On the self-hosted runner the e2e shards run one at a time (`max-parallel: 1`, an expression on the matrix):
every shard's box uses the host network (ports 8787/8788, 25565/25575) and the same container names, so two on
one machine would clash. Each shard removes its boxes at the end and any leftover ones before it boots, and
starts from an empty config repo. Two runs (two refs) on the same machine still clash; give it one runner.
Wall time there is the shards' sum.

## Keeping a config repo's minutes low

A config repo runs only the reusable `server.yml`. What it does to keep minutes down (v1.2):

- **No deploy when nothing deploy-relevant changed.** After a successful deploy the deploy job saves a small
  record to the Actions cache (`scripts/deploy_record.py`: the product sha + a fingerprint of the config files a
  deploy applies). On a push to main or the weekly slot, validate restores the newest record and skips the deploy
  job if nothing changed. Markdown, `.gitignore`, `.env.example`, `expiry.yml` and `.github/` files other than the
  workflow don't count. Never skipped: a manual dispatch of `deploy` (the way to force one, e.g. after
  `mcctl secrets rotate` deployed locally, or to correct drift), a push that changes the workflow (the product
  pin), a missing or unreadable record (first deploy, or the cache entry evicted after 7 days without use).
- **The weekly backup test runs on the server**, not on Actions: `mc-backup-check.timer` (Mondays 09:41 UTC,
  as `mcops`, `scripts/backup_check.py`: `restic check` on 5% of the data, then the newest snapshot restored into
  a private scratch directory and verified, < 2 backup periods old). A failure alerts Discord in the usual
  template. The Monday Actions run is now `expiry-check` only; `backup-check` stays as a manual dispatch task
  (a test from outside the server).
- **pip's download cache** in every job that installs from a hashed requirements file (pip checks every hash).
  Not in `update-test` / `boot-test` (they boot third-party jars, and a cache they saved would be restored on
  main), and no galaxy cache in `deploy` (an installed collection is not hash-checked when reused, and `deploy`
  holds every secret).
- **validate stays a job of its own** before `deploy`: the deploy job's environment (`deploy` with
  `github.environments`, none without; it decides the tailnet's OIDC subject) must be known before the job
  starts, and only server.yml says which. A one-job fold would need the caller to repeat that setting. The PR
  checks (`server / validate`, `server / gate`) stay two jobs too: both are required by the ruleset.

### What each event costs (rounded up per job)

| Event | Jobs | Minutes |
|---|---|---|
| Pull request push | `validate` (1) + `gate` (2) | ~3 |
| Push to `main`, deploy-relevant change | `validate` (1) + `deploy` (~7) | ~8 |
| Push to `main`, nothing deploy-relevant (README, docs) | `validate` | 1 |
| Weekly slot, no updates | `validate` + `update-test` (2) + `update` (2); deploy skipped | ~5 |
| Weekly slot, new builds | `validate` + `update-test` with boot test (~8) + `update` (2) | ~11 |
| Monday | `validate` + `expiry-check` (1) | 2 |
| Manual `deploy` / `backup-check` | `validate` + the task (~8 / ~8) | ~9 |

A typical week (one config change through a PR with two pushes, one lock update PR auto-merged):

- before (v1.1): PR 2 x 3 = 6, merge 8, slot 1 + 8 + 2 + 7 = 18, update PR 3, its merge 8, Monday
  1 + 8 (backup-check) + 1 = 10: **53 minutes**;
- now: PR 6, merge 8, slot 11 (the slot's deploy is skipped: main was deployed by the last merge), update PR 3,
  its merge 8, Monday 2: **38 minutes**.

An idle week: before 12 (slot, with a deploy of an unchanged main) + 10 (Monday) = **22**; now 5 + 2 = **7**
(~30 a month).

**Batch your config changes into one push.** Every push to main that changes something costs a validate and a
deploy (~8): three small pushes are ~24 minutes, the same three edits in one push ~8. On a pull request, each push
costs ~3: push when it's ready, not after every edit. A public config repo costs nothing at all (but its history
is public: SECURITY.md).
