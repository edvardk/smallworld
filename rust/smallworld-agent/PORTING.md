# smallworld-agent: porting contract

The Rust port of the on-box agent: `scripts/mc_agent.py` plus the agent half of `scripts/mc_panel.py`, and the
helpers they run in-process (`notify.py`, `worlds.py`, `health.py`, `alerts.py`, `mc_rcon.py`). This file is the
contract between the people (and agents) porting it in parallel: which Python each module ports, its interface,
what it must pass, and the rules everyone follows. Line numbers are `scripts/mc_agent.py` unless prefixed
`panel:` (`scripts/mc_panel.py`), at the `rust-agent` branch point (v1.4, rust-rcon).

**Python is the spec.** Copy its logic, don't redesign. Every refusal text, audit entry, file (content, mode,
type), RCON line, systemctl/journalctl argv, Worker request (semantic JSON) and Discord text must match. The only
divergences are listed under "Divergences"; add one only with a reason and a test.

## Status

| Module | Owner | Status |
|---|---|---|
| `smallworld-core` (rust/smallworld-core) | phase 1 | done: RCON, server.properties, `pystr`, `pyjson`, `fsx`, `flock`, `audit`, `paths` |
| `cli.rs` | phase 1 | **done**, differential tests in `tests/test_rust_agent_cli.py` |
| `types.rs`, `sys.rs`, `unicode.rs`, `settings.rs`, `http.rs`, `notify.rs`, `systemd.rs`, `worlds.rs` | phase 1 | done (shared) |
| `jobs.rs` | jobs | runner done (foreground + background, cancel); daemon wiring of reports open |
| `panel/plugins.rs`, `panel/pending.rs` | phase 1 / sync | done (`record`, `record_world`, `update`); `merge_sync` is sync's |
| `actions/` | actions | restart, plugin, moderate, world_* done; the rest stubs |
| `link/`, `daemon.rs` | link | stubs |
| `ship/` | ship | stubs |
| `data/`, `panel/rejected.rs` | data | stubs |
| `sync/` | sync | stubs |
| `misc/` | misc | stubs |

Stubs return `AgentError::NotPorted("module::fn")` (or, where the final signature is infallible, a safe
placeholder documented as such, e.g. the scrubber redacts everything). `daemon::NOT_PORTED` lists the modules
still stubbed; the daemon refuses to start while it is non-empty. **Remove your module's name when it is done.**

## Rules for every module

- **Touch only your files.** Shared files (`types.rs`, `sys.rs`, `unicode.rs`, `http.rs`, `systemd.rs`,
  `notify.rs`, `worlds.rs`, `settings.rs`, `main.rs`, smallworld-core) change only in a commit of their own with
  a one-line note in this file's "Shared changes" log. Adding a function is fine; changing a signature needs
  the other owners' callers fixed in the same commit.
- **Errors** are `types::AgentError`, sorted by how Python catches them (`Rejected`, `Action`, `Os(class)`,
  `Value(class, msg)`, `Key`, `Crash`, `NotPorted`; table in types.rs). A Python exception nobody catches is
  `Crash("<traceback last line>")`, never a silent default. `From<PyError>` and `From<io::Error>` do the sorting.
- **Text rules** (smallworld_core::pystr): lengths and slices are code points (`prefix_cp`, `slice_cp`,
  `len_cp`); whitespace is Python's (`isspace`, `split_ws`, `strip_ws`, `collapse_ws`; U+001C..U+001F count);
  `re.ASCII`'s `\d` is `[0-9]` (Rust's `\d` is Unicode); a Python `\d` *without* `re.ASCII` is Rust's `\d`, and
  `int()` of it is `unicode::parse_decimal`. NFKC and categories: `unicode.rs`.
- **JSON** (smallworld_core::pyjson): where bytes matter (audit `MC_ARGS`, cli output, pending.json, tellraw)
  use `dumps`/`dumps_sorted`/`dumps_compact`/`string`; objects always come out sorted (serde_json's `Map` is a
  BTreeMap: never enable `preserve_order`). An insertion-ordered Python dict whose bytes matter is built from
  `string()` pieces (see `jobs::Jobs::announce`). Floats print as `repr(float)` (`float_repr`). Elsewhere
  (Worker bodies) semantic equality is enough. `py_str`/`py_repr`/`truthy`/`py_iter`/`py_float`/`as_int`
  reproduce Python's view of values from `json.loads` (bools are ints in `isinstance(x, int)`:
  `as_py_int_incl_bool`).
- **Files**: `fsx::atomic_write` = `mc_panel.atomic_write` (0600 tmp, fchmod, rename); `fsx::replace_write` =
  `tmp.write_text` + optional chmod + `os.replace`; `pyjson::read_json` = `read_json(path, default)` (`None` =
  the default). Paths: `smallworld_core::paths` (never hardcode one twice).
- **Locks**: `flock::take_job_lock` (job.lock, non-blocking) and `flock::ProcessFileLock` (pending.json.lock,
  blocking + in-process mutex) interlock with Python's `fcntl.flock`. Never hold a lock across a network call.
- **Audit**: `smallworld_core::audit::audit(message, &[(field, value)])`, fields in Python's keyword order.
- **HTTP**: `http::request` only (system CA bundle, ring, no redirects, proxies from the environment). Never put
  a token in an error, log line or argv.
- **systemd**: `systemd::systemctl(&[verb, unit], timeout)` on the polkit-allowed pairs only
  (`ansible/roles/base/templates/50-mc-ops.rules.j2`); `systemd::unit_steps`; `systemd::running_units`.
- **Time**: `sys::now()` (time.time), `sys::utc_iso` (`%Y-%m-%dT%H:%M:%SZ` UTC), `sys::local_since`
  (`%Y-%m-%d %H:%M:%S` local, for `journalctl --since`).
- **Threads**: one OS thread per Python thread; every daemon loop round is wrapped in `catch_unwind` (the
  agent's release profile is `release-agent`, `panic = "unwind"`) and logs like Python's
  `except Exception as e: print(f"... {type(e).__name__}: {e}")`.
- **unsafe**: denied crate-wide; only `sys.rs` allows it (libc calls std lacks), each block with a SAFETY note.
- **Dependencies**: none beyond the workspace's (rust/Cargo.toml `[workspace.dependencies]`). A new crate needs
  a reviewed change to `rust/allowed-crates.txt` and `rust/deny.toml` (and is a maintainer decision).
- Lints: `cargo fmt --all --check`, `cargo clippy --locked --all-targets -- -D warnings` (pedantic), `cargo
  test --locked`. Clean before every commit; `python scripts/build_rust.py check` only at integration (binaries
  are rebuilt once, in the final commit of a Rust PR).

## Shared (phase 1, done)

| Rust | Ports | Interface |
|---|---|---|
| core `rcon.rs`, `mcrcon.rs`, `properties.rs` | mc_rcon.py `Rcon`, `connect`, `read_properties`, `read_untrusted`, `unescape`, `strip_formatting` | `mcrcon::connect()`, `connect_with(dir, timeout)`, `Rcon::command(&PyStr, timeout, single)` |
| core `pystr.rs`, `error.rs`, `errno.rs` | Python `str` rules, exception classes | see above |
| core `pyjson.rs` | `json.dumps`/`json.loads`, `read_json` | `dumps(v, &Dumps)`, `dumps_sorted`, `dumps_compact`, `string`, `float_repr`, `loads`, `read_json`, `py_str`, `py_repr` |
| core `fsx.rs` | panel:97 `atomic_write`, `read_json` | `atomic_write`, `replace_write`, `unlink_missing_ok`, `exists`, `read_text`, `tmp_path` |
| core `flock.rs` | panel:204 `Pending.locked`, panel:1104 `Jobs._take_file_lock` | `take_job_lock`, `ProcessFileLock::lock`, `JOB_BUSY` |
| core `audit.rs` | 219 `audit` | `audit`, `audit_to`, `encode`, `decode` |
| core `paths.rs` | the hardcoded paths of notify.py, mc_panel.py, worlds.py, mc_agent.py | constants, `units::*` |
| `types.rs` | `Rejected`, `ActionError`, `ACTIONS`, `COUNTDOWNS`, `ANNOUNCE_AT`, value helpers | `AgentError`, `AResult`, `Command`, `Obj` |
| `settings.rs` | 150-211 `Settings.from_env`, `check_url` | `Settings::from_env(&Env)`, `check_url`, `url_hostname` |
| `http.rs` | 259-292 `request`'s transport; notify.py `_post` | `http::request`, `http::notify_post`, `HttpError` |
| `notify.rs` | notify.py `maintenance`, `admin_stopped`, `fenced`, `discord` | same names |
| `systemd.rs` | 1338 `systemctl`, 1317 `running_units`, panel:1254 `unit_steps` | same names, `splitlines` |
| `worlds.rs` | worlds.py `check_entry`, `read_library`, `load_state`, `version_check`, `tag_value`, constants | same names, `WorldsState`, `Library`, `EntryError` |
| `unicode.rs` | `unicodedata.normalize("NFKC")`, `category`, `int()` of `\d` | `nfkc`, `category`, `is_bad_category`, `decimal_value`, `parse_decimal` |
| `sys.rs` | `os.umask`, `time.strftime`, `signal.signal` | `umask`, `now`, `utc_iso`, `local_since`, `handle_signals` |
| `panel/plugins.rs` | panel:984-1043 `read_lock`, `read_disabled`, `write_disabled`, `read_snapshots` | same names |
| `panel/pending.rs` | panel:193-288 `Pending` | `Pending::{new, load, save, update, record, record_world}` |
| `jobs.rs` | panel:1046-1246 `Job`, `say_text`, `Jobs`, `rcon_command`, `wait_for_rcon` | `Jobs::{new, with_parts, start, cancel, busy, current, set_report}`, `Job::{set_step, public, state, step}`, `Event` |
| `cli.rs` | 2639-2766 `cli_confirm`, `cli_panel`, `cli_main` | `cli::main(argv)`, `cli::run(argv, panel, state_dir, out, signals)` |

## Modules to port

Each module below lists the Python it ports, its Rust files and interface (already declared; keep the
signatures or fix all callers), and what proves it. "Vectors" are the harness's JSON test vectors
(`tests/agent_parity/vectors/`, generated from the Python functions by `scripts/parity_vectors.py` on the
`agent-harness` branch); load them in `#[test]`s with `include_str!` and compare. "Scenarios" are the
harness's black-box runs (`pytest tests/agent_parity --impl=rust --agent-bin=PATH`).

### link (Worker HTTP, WebSocket, verify, handle_command, control loop, daemon wiring)

- Ports: 259-298 `IngestError`, `request`, `post_json`; 1112-1199 `Rejected`, `Nonces`, `sign`, `verify`;
  1946-1969 `handle_command`; 1972-2149 `ws_accept`, `encode_frame`, `WebSocket`; 2275-2306 `Agent.__init__`,
  2394-2405 `report_job`, 2486-2585 `answer`, `ws_session`, `poll_once`, `control_loop`, `run`; 2768-2785 `main`.
- Files: `link/mod.rs` (`Link::request`, `Link::post_json`, `IngestError`), `link/verify.rs` (`Nonces`,
  `sign`, `verify`), `link/command.rs` (`handle_command`), `link/ws.rs` (`ws_accept`, `encode_frame`, a
  `WebSocket` type, `WsSender`), `link/control.rs` (`control_loop`), `daemon.rs` (`Daemon`, `main`).
- Must: `hmac::Mac::verify_slice` or a constant-time compare; nonces persisted exactly like Python (fail closed
  600 s + audit `fail-closed`); WS over rustls with **one I/O thread owning the stream** and a channel for
  senders (the tail sends while the session waits); 5 s / 300 s reconnect; budget-neutral request counts.
- Daemon wiring: threads metrics, logs, control, inventory, panel, selftest, (client); `catch_unwind` per
  round; `sys::handle_signals(&DAEMON_SIGNALS, stop)`; `print(f"mc-agent {VERSION}: pushing to {host}")`.
  `report_job` posts `/ingest/job` and sets `inventory_due`.
- Proof: vectors for `verify` (every refusal), `ws_accept`, `encode_frame` (fixed mask); scenarios 1, 2, 20, 21.

### ship (journal shipping + scrubber)

- Ports: 596-870 `message_text`, `journal_entry`, `for_d1`, `for_archive`, `FIREWALL_PREFIXES`, `_NB`,
  `SECRET_PATTERNS`, `SECRET_ENV`, `SECRET_SUFFIXES`, `Slicer`, `Scrubber`, `known_secrets`, `scrubber_for`,
  `journal_argv`, the batch reader, logs state (`logs.json`), archive (gzip NDJSON), D1 rows + day cap
  (`select_rows`), `ship_logs`; 2475-2490 `Agent.logs_loop`, 2444-2448 `observe_logs`.
- Files: `ship/journal.rs`, `ship/scrub.rs` (`Scrubber::{new, scrub}`, `known_secrets`), `ship/archive.rs`,
  `ship/mod.rs` (`logs_loop`).
- Must: the 3 look-around patterns rewritten for `regex` (no fancy-regex) with manual boundary checks, Unicode
  `\s` and `(?i)` as Python's; known values incl. the RCON password; cursor moves only after both deliveries;
  `Slicer` CPU slicing. SECURITY.md's scrubber claims stay true word for word.
- Proof: vectors for `Scrubber` (corpus + generated), `journal_entry`, `for_d1`/`for_archive`, `select_rows`;
  differential fuzzing (Hypothesis) of `Scrubber`; scenario 18.

### data (metrics, inventory, console tail, client codes, rejected logins)

- Ports: 299-595 `Counters`, the `/proc` parsers, `unit_memory_mb`, `unit_uptime_s`, `parse_tps_all`,
  `player_names`, `stats_rcon`, `rcon_stats`, `collect`, `Spool`, `push_metrics`; mc_rcon.py `PersistentRcon`
  (202-275); health.py `read_gc_log`, `parse_gc`, `GcStats`, `last_heap`, `parse_mspt`, `parse_tps`,
  `parse_proc_stat`, `steal_pct`, `disk_used_pct`; 871-937 `read_json_obj`, `read_json_list`, `_clip`,
  `collect_inventory`; 940-1005 `ConsoleTail`; 1107 `push_inventory`; 2153-2274 `parse_client_request`,
  `ClientCodes`; 2405-2472 `panel_state`, `metrics_loop`, `inventory_loop`; panel:885-980 `mask_address`,
  `RejectedLogins`.
- Files: `data/metrics.rs` (`Counters`, `collect`, `py_round`), `data/health.rs`, `data/persistent_rcon.rs`,
  `data/inventory.rs`, `data/tail.rs`, `data/client_codes.rs`, `data/mod.rs` (loops), `panel/rejected.rs`.
- Must: one persistent RCON connection shared by metrics and `status` (the e2e `rcon` round checks it); Python
  `round()` semantics (`format!("{:.n$}")` matched on a tie corpus; pin with vectors); the code file 0640; the
  client code never logged; the tail scrubbed (ship's `Scrubber`) and sent through link's `WsSender`.
- Proof: vectors for the parsers, `parse_tps_all`, `player_names`, `py_round`, `mask_address`, the rejected
  feed, `parse_client_request`; scenarios 4, 14, 17, 19.

### actions (the remaining do_<action>s)

- Ports: 1419-1452 `do_status`, `do_selftest`, `do_lookup`, `do_console`; 1454-1541 `_stop_now`, `do_stop`,
  `do_start`, `do_backup`; 1543-1565 `do_tail`, `do_cancel`; 1567-1612 `do_whitelist`, `do_rejected_dismiss`;
  1662-1688 `do_player_message` (+ telemetry.py `Messages` 848-900, reading the sidecar's `online.json`,
  stale > 30 s = nobody online); 1690-1745 `do_restore`.
- Files: `actions/power.rs`, `actions/info.rs`, `actions/moderate.rs`, `actions/restore.rs` (reuse
  `actions/world.rs::follow_steps`), the `actions::Telemetry` trait implementation.
- Must: the admin-stopped marker written first (0644, `fsx::replace_write`), then `systemctl stop`; whitelist
  polls `whitelist.json` 10 x 0.5 s (via `read_untrusted`); `Executor.run`'s `Action` -> `Rejected` mapping is
  already in `actions/mod.rs`.
- Proof: vectors for `check_console` (done here: extend with the harness corpus), `_one_line`; scenarios 3, 5,
  7, 8, 10, 15, 16; `tests/test_agent.py`'s "ACTIONS == do_*" extended to the Rust list.

### jobs (job runner integration)

- The runner is ported (`jobs.rs`, unit tests). Left: the daemon's `report` (`POST /ingest/job` via link,
  `inventory_due`), `Panel.jobs` built with `background: true` and `JOB_LOCK_FILE`, the polkit units each job
  uses (already fixed per action), cancel from the panel (`do_cancel`), job report ordering under load.
- Proof: scenarios 7 (countdown, players online, cancel, busy), 9, 10, 11, 23 (cross-implementation locks:
  Python cli against the Rust daemon and the reverse).

### sync (whitelist -> Git, pending.json, Discord texts)

- Ports: panel:117-189 `parse_players`, `dump_players`, `apply_entries`; panel:291-312 `merge_sync`;
  panel:318-501 `GitHubError`, `GitHub`, `github_from_env`; panel:639-831 `selftest_alert`,
  `selftest_back_alert`, `expired_alert`, `rejected_alert`, `world_rejected_alert` (alerts.py `Alert`,
  `render`); panel:705 `revert_live`; panel:722-814 `sync_pending`; panel:834-882 `sync_world` (+ worlds.py
  `set_world_section`, `library_line`, `_q`); 2433-2443 `Agent.panel_loop`.
- Files: `sync/players.rs`, `sync/github.rs`, `sync/round.rs` (`merge_sync`), `sync/alerts.rs`, `sync/mod.rs`
  (`panel_loop`); `Pending::update` for the merge back.
- Must: players.yml byte-equal to `mcctl.schema.dump_players`; Discord texts byte-exact; never hold the
  pending lock across a network call; what was recorded meanwhile wins (`merge_sync`).
- Proof: vectors for `parse_players`/`dump_players`/`apply_entries`, `merge_sync`, `set_world_section`, every
  alert text; scenarios 12, 13, 23, 24.

### misc (self-test, Mojang lookup, versions, budget, warnings, tokens)

- Ports: panel:315-372 `TOKEN_RE`, `token_shape`, `TokenCache`; panel:502-636 `_no_token`, `selftest`,
  `SelfTestState`; 237-256 `Warnings`; 1008-1017 `read_product`; 1032-1104 `lookup_urls`, `Lookup`; 2345-2392
  `github_token`, `run_selftest`, `selftest_loop`, `product_state`; 2588-2636 the budget.
- Files: `misc/token.rs`, `misc/selftest.rs`, `misc/warnings.rs`, `misc/versions.rs`, `misc/lookup.rs`,
  `misc/budget.rs`.
- Must: the token in memory only, never echoed (`token_shape`); the self-test read-only; budget constants equal
  Python's (`schema.budget` >= `daily_budget`, tests/test_agent.py).
- Proof: vectors for `token_shape`/`TOKEN_RE`, `TokenCache` expiry parse, the budget constants; scenarios 5, 6.

## Parity gate (tests/agent_parity)

The harness (`origin/agent-harness`, merged) runs 25 black-box scenarios against `<bin> run` (daemon) or
`<bin> cli ...` inside `unshare -rm` and compares with `tests/agent_parity/golden/`:
`pytest tests/agent_parity --impl=rust --agent-bin=rust/dist/smallworld-agent-x86_64-unknown-linux-musl` (or a
`cargo build` binary). Vectors for the pure functions: `tests/agent_parity/vectors/*.json` (16 files; regenerate
with `scripts/parity_vectors.py --check`). CI runs the scenarios listed in `RUST_PARITY_SCENARIOS` (ci.yml job
`rust`): **add yours there when your module passes them.** Passing now: `cli`.

| # | Scenario(s) | Module(s) |
|---|---|---|
| 1-3 | `verify` (envelope), `nonce_store`, `console`; `auth_settings` | link; actions (console) |
| 4 | `status` | data (+ actions `do_status`) |
| 5 | `lookup` | misc (+ actions `do_lookup`) |
| 6 | `selftest_ok`, `selftest_failures` | misc (+ sync's alert texts) |
| 7-8 | `stop_start`, `countdown_cancel`, `backup` | actions, jobs |
| 9-11 | `plugin`, `restore`, `worlds` | actions, jobs |
| 12-13 | `whitelist_pr`, `pending_expiry`, `world_pr` | sync |
| 14, 18 | `logs_rejected`, `tail` | ship, data |
| 15 | moderate (in `console`/`cli`) | actions |
| 17 | `client_codes` | data |
| 20 | `ws_reconnect`, `poll_fallback` | link |
| 22 | `cli` | done (phase 1) |
| 23 | `locks` | jobs (needs the daemon) |
| 24 | `state_roundtrip` | everyone (integration) |

Gotchas the goldens encode (keep them; both are logged as known issues to fix in Python and Rust together):
- error texts carry Python exception names (`IsADirectoryError`, `CalledProcessError`, `TimeoutExpired`): use
  `AgentError::Os(class)` / `errno::io_class`;
- `Jobs._run` reports the final state **before** releasing `job.lock` (jobs.rs does: `work` reports, then `run`
  drops the guard);
- `verify` accepts `iat: NaN` (vector case `nan`). serde_json can't parse a bare `NaN`; the vector feeds it
  through Python's parser, so match the vector's expected result for that case explicitly.
- The harness's TLS fake now uses a CA-signed leaf (rustls refuses a self-signed CA certificate as the server's;
  `fakes.make_cert`).

## Ordering

1. Phase 1 (this commit series): core, shared modules, cli. Everyone branches from `rust-agent`.
2. In parallel: link, ship, data, actions, sync, misc, jobs. Only soft dependencies, through the declared
   interfaces: data's tail needs ship's `Scrubber` and link's `WsSender`; actions' status/tail/selftest/lookup
   need data's `collect`/tail and misc's selftest/lookup (wire them through `Panel`/`Executor` fields, which
   exist); sync's self-test texts are used by misc. Port against the interface; stubs keep everything compiling.
3. Integration (link owns it): `daemon.rs` wiring, `NOT_PORTED` empty, remove `#![allow(dead_code)]` and the stub
   lint allows in main.rs, rebuild dist binaries once, then the Ansible switch (design §5).

## Divergences (documented, deliberate)

- HTTP redirects are refused (a 3xx is `HTTPError`); urllib follows them. No endpoint redirects.
- `json.loads` input serde_json refuses (`NaN`, `Infinity`, lone surrogate escapes) reads as invalid (a file:
  the default; a command: refused). Integers beyond 64 bits become floats.
- Transport error classes are urllib's main ones only (`HTTPError`, `URLError`, `TimeoutError`, `ValueError`).
- An uncaught exception prints `Traceback (most recent call last):`, a placeholder frame and Python's last line,
  not Python's frames.
- `Pending.locked`: an `flock` error on an opened lock file is logged and the write goes on (Python would raise;
  it can't happen on a local file).
- Unicode tables are the crates' (pinned), Python's the interpreter's; parity is over code points assigned in
  the oldest supported table (Unicode 14).
- argv that isn't UTF-8 is decoded lossily (Python: surrogateescape); such an argument can't match `CLI_LOGIN`
  or parse as JSON in either.

## How to test against Python

- Unit tests next to the code; vectors via `include_str!("../../../tests/agent_parity/vectors/<name>.json")`.
- `tests/test_rust_agent_cli.py`: black-box differential tests of the cli, Python and Rust side by side under
  `unshare -rm` (fake root with tmpfs over /var/lib, /opt, /run; fake systemctl/journalctl on PATH; a fake RCON
  server; the journald socket captured). Copy its `Box` fixture for daemon-side differential tests until the
  harness covers them. `SMALLWORLD_AGENT_BIN` points the tests at a `cargo build` binary.

## Shared changes

(One line per change to a shared file after phase 1: date, file, what, who.)

- 2026-11-08, `panel/pending.rs`: `Pending::snapshot()` (= `with pending.locked(): pending.load()`), additive. sync.
- 2026-11-08, `tests/agent_parity/fakes.py` `make_cert`: a CA + leaf instead of a self-signed CA certificate
  (rustls refuses the latter). phase 1.
- 2026-11-08, core `flock.rs` tests and `jobs.rs` test `the_file_lock_is_shared_across_runners`: retry the
  re-acquire (a concurrent test's fork shares the lock's descriptor until its exec). Tests only. phase 1.
