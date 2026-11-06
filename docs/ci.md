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
| Pull request, docs only (`docs/**` except the brand previews, root `*.md`, `LICENSE`) | `lint` (ruff, yamllint, the tests that read the docs), `gitleaks`, `config-repo / validate` | `e2e` essential half |
| Pull request, code | `lint` (everything), `gitleaks`, `config-repo / validate`, `controller` (3 OS x 3 Pythons); `plugin` / `seeds` if their inputs changed, `actionlint` if a workflow changed | `e2e` essential half; the heavy half too if the change touches what those checks exercise |
| Nightly, 22:07 / 22:37 UTC on `main` | everything | the whole `e2e` |
| Monthly, the 1st, 23:43 UTC | | the four benches, if the month's commits touched what they measure |
| `release.yml` (tag push) | everything, including `controller` | |
| `workflow_dispatch` | everything, including `controller` | `scope`: `full` (default) / `essential` / `none`; `bench: true` adds the benches |
| Push to `main` | nothing (the pull request already tested the merge commit; the nightly run catches two PRs that break only together) | nothing |

The path groups live in one place, `scripts/ci_changes.py` (`code`, `workflows`, `plugin`, `seeds`, `e2e`, `e2e_full`),
tested by `tests/test_ci_changes.py`. When the change set can't be known (a dispatch, a tag, a git error) the
answer is "everything changed".

- **The essential e2e half:** `mcctl init` twice and the stale-pin move, validate + render, terraform validate,
  `site.yml` twice (the second must report `changed=0`), the status ping, the hardening checks, the agent and
  panel actions, the reboot gate, the deploy checks, the firewall ruleset, the P12 dry runs.
- **The heavy half:** experience telemetry (plugin, agent, real Worker code), branding, migrate container ->
  container, the P16 knobs. It runs on a pull request that touches `plugin/`, `watchdog/`, `server-config/`,
  `tests/e2e/`, the lock files, `ansible/site.yml`, the `base`/`java`/`minecraft`/`plugins`/`backups` roles, or
  the migrate/restore/telemetry code, and in the nightly run.
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
`gitleaks`, `actionlint`, `config-repo / validate` and `e2e`. A workflow skipped by a `paths:` filter never
reports, so a required check would wait forever. Neither workflow therefore has a path filter: a small
`changes` job decides, and

- `lint`, `gitleaks` and `config-repo / validate` always run (`lint` cheaply on a docs-only change; the
  reusable-workflow call would report under another name if it were skipped);
- `actionlint` is skipped by its `if:` when no workflow changed (a skipped job reports success); `e2e` runs on
  every pull request;
- if `changes` itself fails, its outputs are empty, and every `!= 'false'` condition runs the job.

`tests/test_ci_changes.py` checks all three.

## The controller on every OS

`controller` (ci.yml) runs on `ubuntu-24.04`, `macos-15` and `windows-2025` x Python 3.12 (`MIN_PYTHON`), 3.13 and
3.14, on pull requests that touch code and nightly. Linux runs the whole suite. macOS and Windows run
`pytest -m "controller and not posix_only"`:

- `controller` is set by `tests/conftest.py` on the files in `CONTROLLER_TESTS`: the controller's pure logic
  (schema, render, the gate, update PRs, the pin move, workflow secrets, metrics, providers through recorded HTTP).
  The flows that drive ssh, terraform, Ansible or fake binaries (init, deploy, doctor, tf, p12, galaxy, ui) and
  the box's scripts (fcntl, pwd, grp) are left to Linux. On Windows the other test files aren't even collected.
- `posix_only` marks a test in those files that needs POSIX (a `#!/bin/sh` fake binary, a 0600 file mode,
  ansible-playbook). It is skipped on Windows. Mark a test rather than deleting it when a new cell fails.
- mcctl itself refuses native Windows (its preflight says to use WSL2); `mcctl --help` proves the CLI starts
  there. `PYTHONUTF8=1`: Windows' Python reads text as cp1252 otherwise.

Not a required check.

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

No minutes are billed (public repo). For wall-clock planning: a docs-only PR ~25 minutes (the essential e2e), a
code PR ~35-55 (e2e; the controller matrix runs in parallel, ~5-10 per cell), the nightly run ~70, the benches
~110.

### An optional self-hosted runner for e2e

The `e2e` job can move to your own machine (faster, or for a private fork that pays for minutes): set a repository variable `E2E_RUNNER`
(Settings > Secrets and variables > Actions > Variables, e.g. `self-hosted`). The workflow uses it only for
trusted code: the schedule, `workflow_dispatch` and pull requests whose head branch is in the product repo
itself (`github.event.pull_request.head.repo.full_name == github.repository`). A pull request from a fork always
runs on `ubuntu-24.04`. A self-hosted runner executes the branch's code with `--privileged` Docker and `sudo` on
that machine, so on a public repo also turn on Settings > Actions > General > "Require approval for all external
contributors", and give push access only to people you would let run code there. The machine needs Docker,
passwordless `sudo` (`modprobe`), cgroup v2, Python 3.13 and the Terraform CLI that `setup-terraform` installs.
Use a throwaway VM, not your desktop. Delete the variable to go back to GitHub's runners.

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
