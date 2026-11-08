# smallworld-agent: the Rust agent, an overview

`smallworld-agent` (v1.4, `rust/smallworld-agent`) is the Rust port of the on-box agent: `scripts/mc_agent.py`
plus the agent half of `scripts/mc_panel.py`, and the helpers they run in-process (`notify.py`, `worlds.py`,
`health.py`, `alerts.py`, `mc_rcon.py`). It is behind a switch, `server.yml` `agent.implementation: python | rust`
(default `python`; the default becomes `rust` in a later release, after a soak).

This page is the map for someone who wants to learn the code. The contract the port was built against (which
Python each module ports, the rules, the documented divergences, how each part is proved) is
[`rust/smallworld-agent/PORTING.md`](../../rust/smallworld-agent/PORTING.md); the first, smaller walkthrough of
Rust for Python programmers is [smallworld-rcon.md](smallworld-rcon.md). Read that one first.

## Contents

- [What it does](#what-it-does)
- [On the box](#on-the-box)
- [The telemetry sidecar](#the-telemetry-sidecar)
- [How the crate is laid out](#how-the-crate-is-laid-out)
- [Python is the spec: how parity is proved](#python-is-the-spec-how-parity-is-proved)
- [Dependencies and the supply chain](#dependencies-and-the-supply-chain)
- [Build, test and run it yourself](#build-test-and-run-it-yourself)
- [Where to start reading](#where-to-start-reading)

## What it does

Everything mc-agent does (docs/reference.md "mc-agent"), byte for byte where bytes matter:

- the **Worker link**: metrics pushes, the poll fallback and the WebSocket for signed commands, HMAC
  verification with persisted nonces, the job reports;
- every **panel action** (`do_<action>`), the job runner (countdowns, one job at a time across processes:
  `job.lock` interlocks with Python's `fcntl.flock`), `pending.json`, the whitelist -> Git sync and its Discord
  texts, the self-test and the Mojang lookup;
- the **data plane**: metrics with one persistent RCON connection, journal shipping (the archive, D1 rows and the
  scrubber), the inventory, the console tail, `/modpack` codes, the rejected-logins feed;
- `cli`, the entry point `mcctl`'s actions run over SSH (`mc-agent cli mcctl:USER ACTION [ARGS_JSON]`).

Not ported (it stays Python): the experience telemetry (`scripts/telemetry.py`, below), and everything that runs
behind its own systemd unit (`worlds.py`, `restore.py`, `backup.py`, `mc_panel.py apply-plugins`).

## On the box

| Path | What |
|---|---|
| `/opt/minecraft/bin/smallworld-agent` | the binary for the box's architecture (root, 0755), on every box whichever implementation runs; the base role checks it against `mc_smallworld_agent_sha256` before copying it |
| `/opt/minecraft/bin/mc-agent` | a symlink to `smallworld-agent` or `mc_agent.py` (agent.implementation): what `mcctl` runs, and what `mc-run` names `mc-agent-cli-*` |
| `mc-agent.service` | `ExecStart=/opt/minecraft/bin/smallworld-agent run` in rust mode (`mc_agent.py` in python mode); the same user (`mcops`), groups, sandbox, limits and writable paths either way |
| `mc-telemetry.service` | rust mode with experience telemetry on: `mc_agent.py telemetry` |
| `/opt/minecraft/product-version.json` | `agent: {implementation, sha256}` |

Both implementations read and write the same state files (`/var/lib/mc-agent/*`, `pending.json`, `job.lock`,
the markers), so switching either way is a deploy (docs/operations.md "Switching the agent's implementation").
The parity scenario `state_roundtrip` proves the files written by one are read by the other.

## The telemetry sidecar

`telemetry.py` is 1,100 lines of float aggregation, backfill pacing and segments: the hardest part to prove and
on by default, so v1.4 keeps it in Python. In python mode it runs inside the agent, as before. In rust mode it runs
as `mc-telemetry.service` (`mc_agent.py telemetry`, `make_telemetry` + `telemetry_main`), with the same inputs and
outputs, and three couplings cut:

1. **Who is online** (`player_message`): the sidecar writes `/var/lib/mc-telemetry/online.json`
   (`{uuid: {u, n, tips, join}}`, after every poll, atomic, 0640); the Rust agent reads it and treats a file older
   than 30 s as "nobody online" (`actions/telemetry.rs`). The per-player and broadcast limits stay in the agent.
2. **systemd's per-run resource lines** (the overhead card's oneshot units): the Python agent's logs loop fed
   them to the telemetry; the sidecar reads them itself (`journalctl SYSLOG_IDENTIFIER=systemd`, its own cursor).
3. **The overhead card** measures `mc-telemetry.service` too.

## How the crate is laid out

```text
rust/
  smallworld-core/      shared with smallworld-rcon: RCON, server.properties, Python's str/json rules (pystr,
                        pyjson), atomic files (fsx), flock, the audit journal entry, paths
  smallworld-agent/src/
    main.rs             argv: `cli` -> cli.rs, anything else -> daemon.rs (like mc_agent.py's __main__)
    daemon.rs           Settings, the threads (metrics, logs, control, inventory, panel, selftest, client),
                        each round under catch_unwind, signals
    link/               Worker HTTP + WebSocket (rustls), verify + nonces, handle_command, the control loop
    actions/            every do_<action>, the console allowlist, the sidecar's online map (telemetry.rs)
    jobs.rs, panel/     the job runner, pending.json, plugins, rejected logins
    sync/               players.yml, the GitHub client, the whitelist/world pull requests, alerts
    data/               metrics, health parsers, persistent RCON, inventory, console tail, client codes
    ship/               journal reading, the scrubber, the archive, D1 rows
    misc/               self-test, lookup, versions, budget, warnings, the GitHub token cache
    cli.rs              mcctl's entry point
    types.rs, sys.rs, http.rs, systemd.rs, notify.rs, worlds.rs, settings.rs, unicode.rs   shared helpers
```

`unsafe` is denied crate-wide and allowed only in `sys.rs` (libc calls std lacks: umask, strftime, signals), one
block at a time with a SAFETY note.

## Python is the spec: how parity is proved

The port copies the Python's logic instead of redesigning it: refusal texts, audit entries, files (content, mode,
type), RCON lines, systemctl argv, Worker requests and Discord texts match. Three layers prove it:

- **Vectors** (`tests/agent_parity/vectors/*.json`, generated from the Python functions by
  `scripts/parity_vectors.py`): every pure function, loaded by `cargo test`.
- **Scenarios** (`tests/agent_parity`): 25 black-box runs of the whole daemon or the cli inside `unshare -rm`, with
  a fake Worker, GitHub, RCON server, systemctl and journal, compared with golden traces recorded from Python.
  CI runs every one against the committed release binary (ci.yml job `rust`, `RUST_PARITY_SCENARIOS`).
- **Differential tests and fuzzing**: `tests/test_rust_agent_cli.py` (the cli side by side),
  `tests/test_rust_agent_console.py` and `tests/test_rust_scrub_fuzz.py` (Hypothesis against the hidden, pure
  `selftest-console` / `selftest-scrub` subcommands).

And end to end: the e2e's rust shards (`agent-a`, `agent-b`, `telemetry-brand` with `agent.implementation: rust`)
run the same rounds as the Python ones on a real Debian box.

## Dependencies and the supply chain

smallworld-rcon is std only; the agent needs TLS, JSON, HMAC, gzip and regexes, so it uses a small set of crates
pinned exactly in `rust/Cargo.toml` `[workspace.dependencies]` (each at least two weeks old), listed in
`rust/allowed-crates.txt` (checked against `Cargo.lock` by `tests/test_consistency.py`) and passing `cargo deny`
(`rust/deny.toml`: licences, advisories, sources). A new or bumped crate is a maintainer decision. The binaries
are rebuilt byte for byte by `scripts/build_rust.py check` (pinned toolchain, clang 18.1.3 for ring) and pinned in
`ansible/group_vars/all/main.yml`. SECURITY.md "The Rust agent" has the security view.

## Build, test and run it yourself

```sh
cd rust
cargo fmt --all --check && cargo clippy --locked --all-targets -- -D warnings && cargo test --locked
cargo build --locked -p smallworld-agent            # target/debug/smallworld-agent
cd ..
# the parity scenarios against your build (or the committed rust/dist binary)
pytest tests/agent_parity --impl=rust --agent-bin=rust/target/debug/smallworld-agent
# the differential tests and the fuzzing with your build instead of the committed binary
SMALLWORLD_AGENT_BIN=$PWD/rust/target/debug/smallworld-agent pytest -q tests/test_rust_agent_cli.py \
  tests/test_rust_agent_console.py tests/test_rust_scrub_fuzz.py
# after changing the source: rebuild the committed binaries, pin the printed sha256s in group_vars, check
python scripts/build_rust.py build && python scripts/build_rust.py check
```

## Where to start reading

1. `main.rs` and `cli.rs`: the smallest complete path (argv, a JSON argument, the Executor, one audit entry).
2. `link/verify.rs`: HMAC verification and the nonce store, next to `mc_agent.py`'s `verify`.
3. `actions/console.rs`: the console allowlist and its 230 vectors.
4. `ship/scrub.rs`: the scrubber's look-around patterns rewritten for the `regex` crate, with manual boundary
   checks.
5. `daemon.rs`: how the threads are started, restarted and stopped.
