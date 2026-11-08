# smallworld-agent: porting contract

The Rust port of the on-box agent: `scripts/mc_agent.py` plus the agent half of `scripts/mc_panel.py`, and the
helpers they run in-process (`notify.py`, `worlds.py`, `health.py`, `alerts.py`, `mc_rcon.py`). This file is the
contract between the people (and agents) porting it in parallel: which Python each module ports, its interface,
what it must pass, and the rules everyone follows. Line numbers are `scripts/mc_agent.py` unless prefixed
`panel:` (`scripts/mc_panel.py`), at the `rust-agent` branch point (v1.4, rust-rcon). The telemetry module ports
`scripts/telemetry.py` (v1.4.0, "telemetry" below).

**Python is the spec.** Copy its logic, don't redesign. Every refusal text, audit entry, file (content, mode,
type), RCON line, systemctl/journalctl argv, Worker request (semantic JSON) and Discord text must match. The only
divergences are listed under "Divergences"; add one only with a reason and a test.

## Status

| Module | Owner | Status |
|---|---|---|
| `smallworld-core` (rust/smallworld-core) | phase 1 | done: RCON, server.properties, `pystr`, `pyjson`, `fsx`, `flock`, `audit`, `paths` |
| `cli.rs` | phase 1 | **done**, differential tests in `tests/test_rust_agent_cli.py` |
| `types.rs`, `sys.rs`, `unicode.rs`, `settings.rs`, `http.rs`, `notify.rs`, `systemd.rs`, `worlds.rs` | phase 1 | done (shared) |
| `jobs.rs` | jobs | **done**: runner (foreground + background, cancel, announcements); the daemon's reports are link's `daemon::report_job` |
| `panel/plugins.rs`, `panel/pending.rs` | phase 1 / sync | done (`record`, `record_world`, `update`); `merge_sync` is sync's |
| `actions/` | actions | **done**: every `do_<action>`, the `Telemetry` trait (implemented by the telemetry module), console vectors + differential fuzzing; daemon scenarios match golden in their action sections, full gate waits for data (below) |
| `link/`, `daemon.rs` | link | **done** (verify/ws vectors; envelope, nonce, WebSocket and poll sections of scenarios 1, 2, 20 match golden with stubs elsewhere); final wiring of selftest/client loops waits for misc/data |
| `ship/` | ship | **done**: vectors, differential fuzzing (`tests/test_rust_scrub_fuzz.py`), scenarios `logs_rejected`, `tail` |
| `data/`, `panel/rejected.rs` | data | **done**: vectors parsers/round/rejected/constants/client requests + a Python differential corpus; scenario 17 (`client_codes`) passes on its own; 4 (`status`), 14 (`logs_rejected`), `tail` and 24 (`state_roundtrip`) pass in a local merge with ship + actions |
| `sync/` | sync | **done**: players.yml, GitHub client, round (`sync_pending`, `sync_world`, `merge_sync`, `revert_live`), alerts, `panel_loop`; vectors players/merge_sync/worlds/texts; mixed Python+Rust writers test (`sync/mixed.rs`). Scenarios 12 (`pending_expiry`, `world_pr`) and 23's pending.json half match their goldens in everything sync writes; they still wait on data (startup pushes) and actions (`whitelist`, `cancel`) |
| `misc/` | misc | **done** (vectors pass; wiring for the daemon: `misc::Misc`, see "misc" below) |
| integration | integration | **done** (v1.4): the dist binaries rebuilt with every module, all 25 scenarios in `RUST_PARITY_SCENARIOS`, the deploy switch (`agent.implementation`, "Integration" below) |
| `telemetry/` | telemetry | **done** (v1.4.0): telemetry.py + `make_telemetry`, in-process in the daemon; `smallworld-agent telemetry` = `mc_agent.py telemetry`; all 13 telemetry scenarios and every vector group pass; agent scenario `experience` proves the in-process wiring ("telemetry" below) |

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
- **As ported** (for the other modules):
  - `daemon::Daemon` fields: `settings`, `stop: Arc<Event>`, `link: Link`, `inventory_due: Arc<Event>`,
    `warn: &'static Warnings` (= `misc::warnings::WARN`, the one registry, as Python's module-level `WARN`), `ws_connected`/`ws_established: AtomicBool`, `ws` (the live
    session's `Arc<WsHandle>`; `Daemon::ws_sender()` is `Agent.ws` for the console tail), `nonces`, `panel:
    Arc<Panel>`, `executor: Mutex<Executor>`, `misc: Arc<Misc>` (self-test, lookup, product_started). `Daemon::new` returns
    `Err(ValueError text)` for a bad GITHUB_API_URL / MOJANG_LOOKUP_URLS (`main` prints a traceback, exits 1). A module needing more daemon-wide state (counters, spool, the
    self-test state, GitHub, the tail) adds a field built in `Daemon::new`, in a small commit of its own.
  - `Daemon::new` leaves for their owners: `Executor.tail` (data:
    `ConsoleTail.request`), `Executor.telemetry` (actions). `daemon::run` spawns metrics, logs, control,
    inventory, panel, selftest (`daemon::selftest_loop`); **data declares
    `data::client_codes::client_loop(&Daemon) -> AResult<()>`** (started when `settings.client`), then they are
    added to `run`'s list (commented there).
  - Loops wrap each round in `daemon::round("metrics round failed", || ...)` (prints `{what}: {class}: {e}`,
    a panic as `panic: <text>`). A loop thread that panics or returns `Err` is logged and restarted after 5 s;
    `Err(NotPorted)` ends it.
  - `link::command::handle_command` returns `AResult<Value>`: `Err` only for what Python doesn't catch there
    (`Value`/`Key`/`Crash`), e.g. a non-ASCII `sig` (`hmac.compare_digest`'s `TypeError`), which ends the
    WebSocket session (a `ValueError` is swallowed there) or the poll round, exactly as in Python.
  - `jobs::Event` has no `clear()` yet; `inventory_loop` (data) needs one (`inventory_due.clear()`).
  - **Test-only stubs switch:** a debug build starts the daemon despite `NOT_PORTED` when
    `SMALLWORLD_AGENT_ALLOW_STUBS=1` (ignored in release builds: `cfg!(debug_assertions)`). The harness passes a
    fixed environment, so point `--agent-bin` at a wrapper script:
    `printf '#!/bin/sh\nSMALLWORLD_AGENT_ALLOW_STUBS=1 exec %s "$@"\n' $CARGO_TARGET_DIR/debug/smallworld-agent`.
  - wss:// is proven by hand against a TLS server (`link::ws::tests::wss_round_trip`, ignored; see its comment).

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
- **As ported:**
  - `ship::logs_loop(&Daemon)` (the daemon's `LoopFn`) = `run_logs_loop(settings, link, stop, WARN, observe)`
    with `observe` = `ship::observe_logs` (`data::observe_logs(&panel.rejected, ..)`, true sets
    `inventory_due`). `ship_logs(s, &dyn Ingest, path, ShipParts{read, scrub, observe, cede})` is testable
    without a daemon (`Ingest` is implemented for `Link`). `scrubber_for(s)` is rebuilt per batch, as in
    Python (known values re-read every time); the console tail uses `scrub::scrubber_for` and
    `journal::journal_entry` (the tail's view: a `ValueError` reads as None).
  - Each `SECRET_PATTERNS` entry is its look-around-free body plus checks of the characters around the match
    (`scrub.rs` explains why that is exact); `(?i)` and `\s` are spelled out from Python's tables (U+0130,
    U+0131, U+017F, U+212A; U+001C..U+001F), checked over every code point. `scrub::PY_PATTERNS` holds
    Python's sources (the vectors check them).
  - Hidden `smallworld-agent selftest-scrub` (test-only): stdin `{"known", "texts", "journal"}` -> stdout
    `{"texts": scrubbed, "journal": journal_entry results}`; a pure function of stdin (no env, settings or
    files), so it can only print what it was given, scrubbed. `tests/test_rust_scrub_fuzz.py` (Hypothesis,
    derandomized, 300 examples; `SCRUB_FUZZ_EXAMPLES`, `SCRUB_FUZZ_RANDOM=1`) skips until the dist binary has
    it (after the integration rebuild) or `SMALLWORLD_AGENT_BIN` points at a cargo build.
  - `observe_all` (telemetry's overhead card, v1.4.0): `ShipParts.observe_all` sees every entry of a batch before
    scrubbing; `logs_loop` passes the in-process telemetry's `Overhead::observe_journal` when `EXPERIENCE=1`.
  - Fixed in both (v1.4 integration): a journal `PRIORITY` of digits `int()` refuses (`"²"`, which `isdigit()`
    accepts; more than 4300 digits) used to raise an uncaught `ValueError` in `read_journal`, so every log round
    failed at that line and the cursor never passed it (a local user could stall shipping with a native journal
    field). Both now read it as the default priority 6 (`mc_agent.journal_priority`, `ship::journal::priority`);
    `try_journal_entry` returns `Option<Entry>`. Tests: `tests/test_agent.py`
    `test_a_hostile_priority_cannot_stall_log_shipping`, `journal::tests::python_edge_cases`, the fuzzing.

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
- **As ported** (for the other modules):
  - Daemon entries (`daemon::run`): `data::metrics_loop`, `data::inventory_loop`,
    `data::client_codes::client_loop` (when `settings.client`), and `data::wire(&Arc<Daemon>)` before the loops
    (the executor's tail: `ConsoleTail` reading `Daemon::ws_sender`, scrubbed with ship's `Scrubber::new(
    known_secrets(..))` per run). `Daemon::new` takes `data::status_sample()` (`collect(Counters())`).
    Counters and spool live in data (`data::metrics_state`, one daemon per process).
  - `data::PANEL_EXTRAS` (set in `daemon::run`): sync's `self.github is not None` and misc's `product_state()`
    / `selftest_state.public()` for `panel_state`.
  - **ship's wiring (open):** `Agent.observe_logs` is `data::observe_logs(&d.panel.rejected, entries)` (true:
    set `inventory_due`); pass it as `run_logs_loop`'s `observe`. Proven in the local merge (scenario 14).
  - `RejectedLogins`: `entries()` (= Python), `feed(&[(ts_ms, msg)])` (= Python), `drop_name`; `load`/`observe`
    are kept for the declared interface.
  - The PersistentRcon is shared through `data::persistent_rcon::stats_rcon(server_dir)` (one per dir, also
    for `status`); `RconFailure` sorts errors as `rcon_stats` catches them.
  - The loops take explicit arguments (`run_metrics_loop`, `run_inventory_loop`, `ClientCodes::run`) so they
    can be tested without a daemon.

### actions (the remaining do_<action>s)

- Ports: 1419-1452 `do_status`, `do_selftest`, `do_lookup`, `do_console`; 1454-1541 `_stop_now`, `do_stop`,
  `do_start`, `do_backup`; 1543-1565 `do_tail`, `do_cancel`; 1567-1612 `do_whitelist`, `do_rejected_dismiss`;
  1662-1688 `do_player_message` (telemetry.py `Messages` and `online()` now come from the telemetry module);
  1690-1745 `do_restore`.
- Files: `actions/power.rs`, `actions/info.rs`, `actions/moderate.rs`, `actions/restore.rs` (reuse
  `actions/world.rs::follow_steps`), the `actions::Telemetry` trait implementation.
- Must: the admin-stopped marker written first (0644, `fsx::replace_write`), then `systemctl stop`; whitelist
  polls `whitelist.json` 10 x 0.5 s (via `read_untrusted`); `Executor.run`'s `Action` -> `Rejected` mapping is
  already in `actions/mod.rs`.
- Proof: vectors for `check_console` (done here: extend with the harness corpus), `_one_line`; scenarios 3, 5,
  7, 8, 10, 15, 16; `tests/test_agent.py`'s "ACTIONS == do_*" extended to the Rust list.
- Done (actions owner): `actions/info.rs` (status, selftest, lookup, console, tail, cancel, player_message),
  `power.rs` (stop + `stop_now`, start, backup), `moderate.rs` (whitelist + `read_json_list`, rejected_dismiss),
  `restore.rs`, `telemetry.rs`. Vectors: `console.json` (all 230) and `one_line.json` in `cargo test`;
  `tests/test_rust_agent_console.py` fuzzes the allowlist against Python (Hypothesis) through the hidden
  `selftest-console`; `tests/test_agent.py::test_rust_agent_actions_match_python` keeps ACTIONS, the dispatch and
  the `do_*` set equal and stub-free.
- **For the daemon wiring (link):** `Executor::new(sample, tail, panel, telemetry)` with `sample` = data's
  `collect(Counters())` (a fresh `Counters`, the shared persistent RCON), `tail` = data's `ConsoleTail.request`
  (always passed; it refuses itself without the WebSocket), `panel.selftest`/`panel.lookup` = misc's, `panel.changed` = set `inventory_due`;
  `telemetry` = the in-process `telemetry::Telemetry` when `EXPERIENCE=1` (`telemetry::make_telemetry`; a bad
  `SEGMENT_*`/`BACKFILL_PAUSE_S`/`MESSAGE_*` value is Python's `ValueError` in `Agent(s)`: a traceback, exit 1),
  else `None`.
- (v1.4.0) `online.json` is gone from the agent: `player_message` reads `Telemetry.online()` in-process. Only
  `smallworld-agent telemetry` (the harness's entry point, as `mc_agent.py telemetry`) still writes it.
- Scenario status (2026-11-08, debug build behind the stub gate, on top of link + sync + misc): with a local,
  uncommitted harness tweak that skips the startup metrics/inventory pushes (data) and drops the data plane's
  trace parts (inventory notes and pushes, `rcon.stats_connections`, `systemd.probes`, spool.jsonl), these match
  golden on `notes`, `exits`, `rcon`, `systemd`, `audit`, `files`, job reports and WebSocket results:
  `console`, `stop_start`, `countdown_cancel`, `backup`, `plugin`, `restore`, `worlds`, `lookup`, `selftest_ok`,
  `selftest_failures`, `locks`, `whitelist_pr` (the last one with `RejectedLogins::drop_name` returning
  `Ok(false)` locally: data's stub refuses it). `cli` passes unmodified. **Not yet in `RUST_PARITY_SCENARIOS`:**
  the gate runs the release dist binary, which refuses to start while `NOT_PORTED` holds ship and data, and
  every daemon scenario waits for data's startup pushes; add them with data's merge and the dist rebuild.
  `status`, `tail`, `logs_rejected` (rejected_dismiss) need data's `collect`, `ConsoleTail`, `RejectedLogins`.

### jobs (job runner integration)

- **Done** (see Status): link's `Daemon::new` builds the runner as below and reports through
  `daemon::report_job`; a SIGTERM in the daemon just stops it (Python's job threads are daemon threads: nothing
  more is reported, the kernel drops job.lock); the cli cancels a countdown (cli.rs). Was left: the daemon's `report` (`POST /ingest/job` via link,
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
- Interface (as ported): `sync::github::{GitHub, TokenSource, FixedToken, github_error, is_github_error,
  github_from_env(env, Option<Arc<dyn TokenSource>>)}`. `GitHubError` is `AgentError::Value("GitHubError", msg)`.
  **misc:** `impl TokenSource for TokenCache` (`token()` = `__call__`, `invalidate()` forgets the token and
  returns true); the self-test's `GitHub(lambda: token, ...)` is `GitHub::new(Arc::new(FixedToken(token)), ...)`,
  `players_on_main()` and `call("GET", path, None)` are public; texts in `sync::alerts`. **link/integration:**
  `sync::panel_loop(&Daemon) -> AResult<()>` is the daemon's `LoopFn` (`("panel", Box::new(crate::sync::panel_loop))`):
  it builds `PanelSync::new(&settings, &process_env(), TokenCache over POST /agent/github-token, inventory_due)`
  and runs `sync::run_panel_loop(&ps, &daemon.panel.pending, &daemon.stop)`.

### misc (self-test, Mojang lookup, versions, budget, warnings, tokens)

- Ports: panel:315-372 `TOKEN_RE`, `token_shape`, `TokenCache`; panel:502-636 `_no_token`, `selftest`,
  `SelfTestState`; 237-256 `Warnings`; 1008-1017 `read_product`; 1032-1104 `lookup_urls`, `Lookup`; 2345-2392
  `github_token`, `run_selftest`, `selftest_loop`, `product_state`; 2588-2636 the budget.
- Files: `misc/token.rs`, `misc/selftest.rs`, `misc/warnings.rs`, `misc/versions.rs`, `misc/lookup.rs`,
  `misc/budget.rs`.
- Must: the token in memory only, never echoed (`token_shape`); the self-test read-only; budget constants equal
  Python's (`schema.budget` >= `daily_budget`, tests/test_agent.py).
- Proof: vectors for `token_shape`/`TOKEN_RE`, `TokenCache` expiry parse, the budget constants; scenarios 5, 6.
- **Done.** Vectors pass: `tokens.json` (TOKEN_RE, token_shape, expires_at incl. fullwidth digits, `:60`,
  `24:00` fallback), `constants.json` (misc constants, budget model, `daily_budget` cases, Settings clamps),
  `texts.json` `selftest_alert`/`selftest_back_alert`. Self-test follows rust-rcon's fix (`last_panel`: 60 s
  from the last on-demand run). Integration (link's `Daemon::new` / `run`, data's inventory, actions):
  ```text
  let misc = Arc::new(Misc::new(&env, &settings.state_dir, misc::github_token_fetch(link.clone()))?); // wired in Daemon::new
  misc.wire(&mut panel);                                          // wired: Panel.selftest + Panel.lookup
  ("selftest", daemon::selftest_loop)                              // wired (Daemon.misc)
  inventory: "product": misc.product_state(), "selftest": misc.selftest.state.public()?
  do_lookup:  Ok(misc::lookup::reply_json(&lookup(get(args, "name"))?))   // Python's key order, not serde's
  do_selftest: panel.selftest("panel")?, (panel.changed)(), "passed[ (an earlier run: one per minute)]: steps" | "failed at {error or 'an unknown step'}"
  do_cancel:  self.need_panel("cancel")?.jobs.cancel(&self.login)
  ```
  Checked on a throw-away merge with link + sync (stub gate, a fake startup push in place of data's loops, the
  three actions above): `lookup`, `selftest_ok`, `selftest_failures` and `countdown_cancel` match their goldens
  except the inventory/metrics/rcon-stats parts (data) and `countdown_cancel`'s `start`/`stop` busy refusals
  (actions). Not in `RUST_PARITY_SCENARIOS` yet: they need data and actions.
- `selftest.rs` keeps its own copy of alerts.py `render` (`render_alert`) for the two self-test texts; sync's
  alert texts use the same template. Both are pinned by `texts.json`; fold into one at integration.
- `GhRepo::from_env` repeats `github_from_env`'s checks (sync's `GitHub` carries the same repo/api/branch).

### telemetry (scripts/telemetry.py, v1.4.0)

- Ports: telemetry.py whole (`clean_text`, snapshots, `SnapshotReader`, minute rows, `Sessions`, `Overhead`,
  `unit_stats`, stats, `Segments`, `in_window`/`quiet`, `Pacer`, `Backfill`, verdicts, `Messages`, `Config`,
  identity, `Telemetry`), mc_agent.py `make_telemetry` (2286-2323), the daemon's telemetry thread (2594-2596), the
  logs loop's `observe_all` (840-851), and the `telemetry` subcommand (2800-2904: `online_map`, `write_online`,
  `SidecarTelemetry`, `overhead_round`, `overhead_loop`, `telemetry_main`).
- Files: `telemetry/json.rs` (an insertion-ordered value with Python's `json.loads`/`dumps`: segments' NDJSON and
  verdicts.tsv depend on dict order), `snapshot.rs`, `minute.rs` (`py_sum` = CPython >= 3.12 `sum()`), `overhead.rs`,
  `stats.rs`, `segments.rs`, `backfill.rs`, `messages.rs` (was `actions/telemetry.rs`), `mod.rs` (verdicts, Config,
  identity, `Telemetry`, `make_telemetry`), `sidecar.rs` (`smallworld-agent telemetry`), `vectors.rs` (tests).
- Design: **in-process**, as the Python agent. `Daemon.telemetry: Option<Arc<telemetry::Telemetry>>`, built in
  `Daemon::new` (`self.telemetry.stop = self.stop`), its thread `daemon::telemetry_loop`; `Executor.telemetry` is
  the same `Arc` (`impl actions::Telemetry`); the logs loop feeds `Telemetry.overhead`. The loop's state (`Core`)
  sits behind a mutex only its thread takes; `online()` clones the latest snapshot's `Arc` under a lock never held
  across I/O (Python's lock-free read, P13 review L4). Each round is `catch_unwind`, logged on stdout as Python's
  `print(f"telemetry round failed: ...")`. mc-agent.service already had the telemetry's paths (StateDirectory
  `mc-agent mc-telemetry`, the verdict directory, `systemd-journal`), so the sidecar unit went away with no
  sandbox change (a separate unit had no sandbox advantage: the same user, token and paths).
- Proof: `tests/telemetry_parity` (13 scenarios of `<bin> telemetry` against Python's goldens: all pass with
  `--impl=rust`), its vectors in `cargo test` (`telemetry/vectors.rs`: every group, compared as compact JSON
  text), the agent scenario `experience` (the daemon with `EXPERIENCE=1`: minute rows, player_message's online view
  and limits, messages.tsv), e2e `telemetry_check.py` on the rust shard (`E2E_AGENT_IMPL`).
- Python issues kept for parity (the goldens and vectors lock them in; fix in both later, with a test on each side):
  `NaN`/`Infinity` in a snapshot's integer field raises from `int()` (`ValueError`/`OverflowError`: the round fails
  and the reader never moves past that file; Rust raises the same exception, `snapshot::py_int`, vectors
  `python_raises`); a backfill row the Worker refuses with a 4xx is retried forever (the Pacer never gives it up);
  `latest` is never cleared after the plugin goes quiet (`quiet()` and `online()` keep the last snapshot).

## Parity gate (tests/agent_parity)

The telemetry port (v1.4.0, `scripts/telemetry.py` -> `<bin> telemetry`) has its own gate,
`tests/telemetry_parity` (docs/ci.md "The telemetry parity harness"): `pytest tests/telemetry_parity --impl=rust
--agent-bin=PATH` runs its scenarios against `PATH telemetry` (the module the daemon runs in-process); vectors in
`tests/telemetry_parity/vectors/*.json` (`scripts/telemetry_vectors.py`).

The harness (`origin/agent-harness`, merged) runs 25 black-box scenarios against `<bin> run` (daemon) or
`<bin> cli ...` inside `unshare -rm` and compares with `tests/agent_parity/golden/`:
`pytest tests/agent_parity --impl=rust --agent-bin=rust/dist/smallworld-agent-x86_64-unknown-linux-musl` (or a
`cargo build` binary). Vectors for the pure functions: `tests/agent_parity/vectors/*.json` (16 files; regenerate
with `scripts/parity_vectors.py --check`). CI runs the scenarios listed in `RUST_PARITY_SCENARIOS` (ci.yml job
`rust`): **add yours there when your module passes them.** Passing now: `cli`. With a cargo build of the merged tree (ship done, no stubs switch): all 25 scenarios. With a debug build and the stubs switch: `client_codes` (data alone); with ship + actions merged locally: also `status`, `logs_rejected`, `tail`, `state_roundtrip` (add them to `RUST_PARITY_SCENARIOS` with the dist rebuild: the committed binary still refuses to start the daemon).

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
   lint allows in main.rs, rebuild dist binaries once, then the Ansible switch (design §5). Done except the lint
   allows in main.rs: removing them surfaces ~40 unused interface items across the modules (declared for the
   porting contract, unused by the final wiring); a follow-up cleanup, not a behaviour change.

## Integration (v1.4, as deployed)

- `server.yml` `agent.implementation: python | rust` (default `python`, `mcctl/schema.py`) renders
  `mc_agent_implementation`. The base role installs `/opt/minecraft/bin/smallworld-agent` (root 0755, the pin
  asserted first) on every box and points `/opt/minecraft/bin/mc-agent` at the chosen implementation;
  `mc-agent.service` runs `smallworld-agent run` (any first argument but `cli` and the hidden selftests runs the
  daemon, as in Python). mcctl's `agent_command` runs `mc-agent cli`, or `mc_agent.py cli` on a pre-1.4 box
  (`sudo test -x`); `mc-run` names `mc-agent-cli-*` units for all three entry paths.
- Telemetry runs in-process in both implementations (v1.4.0; the pre-release sidecar `mc-telemetry.service` is
  gone: the agent role stops and removes a leftover unit before it installs or starts mc-agent).
- Restarts: a new binary, the symlink or `product-version.json` (`agent: {implementation, sha256}`) notify the
  handler pair `mc-agent code changed`.
- e2e: `agent-a`, `agent-b`, `telemetry-brand` with `impl: rust` in `e2e-mock.yml`.

## Divergences (documented, deliberate)

- HTTP redirects are refused (a 3xx is `HTTPError`); urllib follows them. No endpoint redirects.
- sync: a GitHub reply that isn't JSON is `JSONDecodeError: Expecting value: line 1 column 1 (char 0)` whatever
  the position (pending.json's `error` then says so); a reply body in UTF-16/32 (json.loads detects it) is a
  `UnicodeDecodeError`. A players.yml value with a lone surrogate escape is a `ValueError` (Python keeps it).
- sync: a GITHUB_API_URL that `github_from_env` refuses ends the panel loop with one line on stderr; Python's
  `Agent.__init__` raises and the agent doesn't start (link may move the check to `Daemon::new`).
- sync: `sorted(library, key=name)` with non-string names raises a `TypeError` with a generic message.
- `json.loads` input serde_json refuses (`NaN`, `Infinity`, lone surrogate escapes) reads as invalid (a file:
  the default; a command: refused). Integers beyond 64 bits become floats.
- Transport error classes are urllib's main ones only (`HTTPError`, `URLError`, `TimeoutError`, `ValueError`).
- An uncaught exception prints `Traceback (most recent call last):`, a placeholder frame and Python's last line,
  not Python's frames.
- `Pending.locked`: an `flock` error on an opened lock file is logged and the write goes on (Python would raise;
  it can't happen on a local file).
- Unicode tables are the crates' (pinned), Python's the interpreter's; parity is over code points assigned in
  the oldest supported table (Unicode 14).
- `smallworld-agent selftest-console` (hidden, test-only) reads a JSON array on stdin and prints the console
  allowlist's outcomes (`actions::console::selftest_main`); Python's agent has no such subcommand (there, any
  first argument but `cli` runs the daemon). Pure: no RCON, no file, no audit, no program run.
- `read_json_list` (whitelist/ops checks): `json.loads(bytes)`'s UTF-16/32 auto-detection isn't reproduced
  (UTF-8, with or without a BOM, is); the server writes UTF-8.
- telemetry: a minute row's `stats` object lists its players in sorted order; Python's comes from iterating a set
  (`online().keys() | {...}`), which varies with the hash seed. Same content.
- telemetry: a snapshot's integer beyond 64 bits (Python: a big int) saturates where Python `int()`s it, and makes
  a `ts` invalid (the snapshot is skipped); one beyond a float's range reads as an infinity (Python's `float()`
  raises `OverflowError: int too large to convert to float` in `_num` itself); a `NaN` among the values `sorted()`
  or `sort(key=...)` orders may come out in another order; a boolean `ts` (an int to Python) reads as 0/1 where Python echoes
  `true` in the row's `raw`. The plugin writes neither (a hot chunk's boolean `cx`/`cz` is echoed as Python does).
- telemetry: a segment with a corrupt deflate stream is unreadable (given up, a gap), as a truncated or non-gzip
  one is in both; Python raises an uncaught `zlib.error` for it (the round fails). The segments are the agent's own.
- telemetry: `telemetry-id.json` with a non-finite `seq` reads as invalid (a new id); Python's `int()` raises an
  uncaught `OverflowError` and the agent doesn't start. The file is the agent's own.
- telemetry: `sum()` is CPython >= 3.12's (Neumaier-compensated for floats). A Python 3.11 box (Debian 12) sums
  uncompensated; the two can differ in the last bit of a mean before `round()`.
- telemetry: `OSError` texts in the loop's log line are rebuilt from errno and the path (`[Errno 13] Permission
  denied: '...'`); `unit_stats`' undecodable output is `UnicodeDecodeError` with a fixed message.
- telemetry: the Unicode tables are the crate's (newer than the generating Python's 15.1 in places: U+0897 and the
  other code points Unicode 16 assigned are printable here); the vectors check every other code point.
- Self-test probe (misc): a GitHub reply that isn't JSON reads as `ValueError("Expecting value: line 1 column 1
  (char 0)")` whatever the actual JSONDecodeError position; base64 and the subscript errors follow CPython's
  texts for the common cases. Only the step's `detail` text can differ, never the outcome.
- data: `str.isdigit()` checks (systemctl's `MemoryCurrent`, `cpuN`) accept `Nd` digits only; Python's
  `isdigit` also takes superscripts, then `int()` of them raises. A big integer in a /proc or GC-log field
  saturates at i64 (Python: a big int). Neither occurs in real output.
- data: `json.loads` of an untrusted server file accepts UTF-8 (with or without BOM) only; Python's also
  auto-detects UTF-16/32.
- data: the status sample, spool lines and inventory objects have sorted keys (Python: insertion order); only
  their JSON meaning reaches the Worker/harness. rejected.json is written in Python's key order.
- ship: the gzip archive's deflate stream is miniz_oxide's (Python: zlib); the header matches Python's (no
  name, MTIME now, XFL 2, OS 255) and the decompressed NDJSON is byte-identical, which is all the Worker and the
  harness read (Python's bytes differ run to run anyway, by MTIME).
- ship: `Scrubber` replaces known values of equal length in sorted order; Python's order is its set iteration
  order (varies with the hash seed), so this is one of Python's possible outputs (they differ only for two
  overlapping secrets of the same length).
- ship: a journal timestamp (or `logs.json` `rows`) beyond 64 bits: Python keeps the big int, Rust reads the
  timestamp as invalid (entry dropped) and saturates `rows`; a `PRIORITY` beyond 64 bits becomes a float.
  `str()` of a JSON object field prints sorted keys. serde_json may read a float with 16-17 significant digits
  one ulp off (no `float_roundtrip` feature), visible only in `str()` of a float-valued field. journalctl emits
  none of these.
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
- 2026-11-08, `tests/agent_parity/golden/selftest_{ok,failures}.json`: regenerated from Python after merging
  rust-rcon (selftest.json records `last_panel`). misc.
- 2026-11-08, `daemon.rs` `NOT_PORTED`: `misc` removed. misc.
- 2026-11-08, `daemon.rs` misc wiring: `Daemon.misc` (`Misc::new` + `misc.wire(&mut panel)` in `Daemon::new`, which
  now returns `Result`), `Daemon.warn` is `&'static` `misc::warnings::WARN` (callers unchanged), the `selftest`
  loop (`daemon::selftest_loop`). misc.
- 2026-11-08, `tests/agent_parity/fakes.py` `make_cert`: a CA + leaf instead of a self-signed CA certificate
  (rustls refuses the latter). phase 1.
- 2026-11-08, core `flock.rs` tests and `jobs.rs` test `the_file_lock_is_shared_across_runners`: retry the
  re-acquire (a concurrent test's fork shares the lock's descriptor until its exec). Tests only. phase 1.
- 2026-11-08, `sys.rs`: add `monotonic()` (CLOCK_MONOTONIC, for `unit_uptime_s`) and `disk_usage(path)`
  (`shutil.disk_usage`); `systemd.rs`: `run` becomes `pub(crate)` (data's `systemctl show` queries). Additive. data.
- 2026-11-08, `jobs.rs`: add `Event::clear` (`threading.Event.clear`, for `inventory_due`). Additive. data.
- 2026-11-08, `daemon.rs`: `run` starts `data::client_codes::client_loop` when `settings.client` and calls
  `data::wire` (the executor's tail through `ws_sender`) before the loops; `Daemon::new` takes
  `data::status_sample()`; `data` leaves `NOT_PORTED`. data.
- 2026-11-08, core `properties.rs`: `read_untrusted_limit(path, limit)` and `UNTRUSTED_MAX_BYTES` (mc_rcon.py's
  default 16 MiB); `read_untrusted` now calls it with 1 MiB. Additive. actions.
- 2026-11-08, `settings.rs`: `int_env` made `pub` (telemetry's `MESSAGE_*` settings). actions.
- 2026-11-08, `main.rs`: the hidden `selftest-console` subcommand (see "Divergences"). actions.
- 2026-11-08, `requirements/dev.in` (+ `dev.txt`): `hypothesis==6.169.0` for the differential tests (ship's
  scrubber fuzzing needs it too: keep one pin). actions.
- 2026-11-08, `main.rs`: the hidden `selftest-scrub` subcommand (`ship::selftest_scrub`, see ship's notes). ship.
- 2026-11-08, `panel/rejected.rs`: a `feed` stub with data's signature (superseded by data's port at merge). ship.
- 2026-11-08, `daemon.rs`: `NOT_PORTED` is empty (ship done). ship.
- 2026-11-08, `link/mod.rs`: `Link::request_body` (the reply before `json.loads`; `request` = it + loads). Additive.
  telemetry.
- 2026-11-08, `ship/mod.rs`: `ShipParts.observe_all` / `ObserveAll` and `run_logs_loop(.., observe_all)` (Python's
  `ship_logs(observe_all=...)`); `logs_loop` passes the telemetry's overhead. telemetry.
- 2026-11-08, `daemon.rs`: `Daemon.telemetry`, `telemetry_loop`, the executor's `Telemetry` from the module
  (`actions/telemetry.rs` moved to `telemetry/messages.rs`, its online.json reader removed); `main.rs`: the
  `telemetry` subcommand. telemetry.
- 2026-11-08, `tests/agent_parity`: scenario `experience` + its golden (from Python). telemetry.
