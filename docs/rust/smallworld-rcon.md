# smallworld-rcon: a Rust walkthrough for Python programmers

`smallworld-rcon` is the product's first Rust component (v1.4): a drop-in for the **command line** of
`scripts/mc_rcon.py`. It does exactly what `mc_rcon.py cmd|stop|countdown|worldborder|gamerule` does (same
arguments, output, exit status, timeouts, packets) in one static binary with no dependencies. The Python module
stays: about ten scripts `import mc_rcon` and keep doing so.

This page explains the code to someone who knows Python and wants to learn Rust. Read it next to the source in
`rust/smallworld-rcon/src/`; every file is short and commented.

## Contents

- [Why a Rust copy of a working script](#why-a-rust-copy-of-a-working-script)
- [Where it runs](#where-it-runs)
- [How the crate is laid out](#how-the-crate-is-laid-out)
- [The RCON protocol, packet by packet](#the-rcon-protocol-packet-by-packet)
- [The files, one by one](#the-files-one-by-one)
- [The stop path](#the-stop-path)
- [What "identical" means, and how it is proved](#what-identical-means-and-how-it-is-proved)
- [Which Python is the spec](#which-python-is-the-spec)
- [Build, test and run it yourself](#build-test-and-run-it-yourself)
- [Exercises](#exercises)

## Why a Rust copy of a working script

Two reasons. It is a contained, real piece of the product to learn Rust on: a network protocol, a file format,
a command line, error handling and tests, in about 3,000 lines (half of them tests and comments). And a static
binary needs nothing at run time: `ExecStop` keeps working even if the box's Python were broken, which matters
because a bad stop can lose world data.

The rules it follows (CLAUDE.md "Rust workspace"): the toolchain is pinned (`rust/rust-toolchain.toml`), there
are **no crates.io dependencies** (std only, like the stdlib-only server scripts: nothing to audit, no supply
chain), `unsafe` is forbidden, and the committed binaries in `rust/dist/` are rebuilt byte for byte by
`python scripts/build_rust.py check` in CI.

## Where it runs

| Call site | Before | Now |
|---|---|---|
| `minecraft.service` `ExecStop` | `mc_rcon.py stop` | `smallworld-rcon stop`, `mc_rcon.py stop` as fallback ([The stop path](#the-stop-path)) |
| A deploy's restart (`ansible/roles/minecraft/tasks/restart.yml`) | `mc_rcon.py countdown 60` | `smallworld-rcon countdown 60` |
| World borders, game rules (`ansible/roles/minecraft/tasks/main.yml`) | `mc_rcon.py worldborder/gamerule` | `smallworld-rcon worldborder/gamerule` |
| The `mc` helper (`mc list`, also behind `mcctl rcon` and `mcctl ssh`) | `mc_rcon.py cmd` | `smallworld-rcon cmd` |

The base role installs it as `/opt/minecraft/bin/smallworld-rcon` (root-owned, 0755), choosing the binary for the
box's architecture (x86_64 or aarch64, the same choice as restic's download) and checking its sha256 against
`mc_smallworld_rcon_sha256` in `ansible/group_vars/all/main.yml` **before** copying it.

## How the crate is laid out

```text
rust/
  Cargo.toml              the workspace: its members and the release profile (how dist/ is built)
  Cargo.lock              committed; lists only our own crate (no dependencies)
  rust-toolchain.toml     the exact compiler version, its components and the two musl targets
  .cargo/config.toml      link with rust-lld (ships with Rust): no C toolchain needed for either target
  dist/                   the committed binaries, one per target
  smallworld-rcon/
    Cargo.toml            the crate: name, edition, lints (unsafe forbidden, clippy pedantic)
    src/
      main.rs             entry point: parse the arguments, run the subcommand, exit status
      args.rs             the command line, parsed by argparse's rules
      ops.rs              connect, stop, countdown, worldborder, gamerule (mc_rcon.py's functions)
      rcon.rs             the protocol: packets and the Rcon connection
      properties.rs       reading server.properties
      pystr.rs            Python's text rules: strings, whitespace, int(), repr()
      error.rs            the error type: one variant per Python exception
      errno.rs            glibc's error texts (Python prints them; the binary links musl)
```

`Cargo.toml` is `pyproject.toml`; `Cargo.lock` is a fully resolved `requirements/*.txt` with hashes (ours is
nearly empty); a *crate* is a package; a *workspace* is several crates sharing one lock file and one `target/`
build directory. Each `src/*.rs` file is a *module*, pulled in by a `mod name;` line in `main.rs`, the way a
Python package imports its own modules.

## The RCON protocol, packet by packet

RCON is Valve's "Source RCON", which Minecraft speaks on `rcon.port` (25575, localhost only in this product). A
packet, in both directions:

```text
offset  size  field
0       4     length: the number of bytes after this field, little-endian i32
4       4     request id, chosen by the client; the server echoes it (-1 = wrong password)
8       4     type: 3 = login, 2 = command, 0 = response
12      n     body, UTF-8
12+n    2     two zero bytes
```

A whole conversation for `smallworld-rcon cmd list`, as the tests record it:

| # | Direction | id | type | body | Why |
|---|---|---|---|---|---|
| 1 | client → server | 1 | 3 login | `pw` | the password from server.properties |
| 2 | server → client | 1 | 2 | (empty) | login accepted (id -1 would mean refused) |
| 3 | client → server | 2 | 2 command | `list` | |
| 4 | server → client | 2 | 0 response | `There are 0 of a max of 20 players online: ` | first (here only) fragment |
| 5 | client → server | 3 | 0 response | (empty) | the "marker": see below |
| 6 | server → client | 3 | 0 | `Unknown request 0` | the marker's answer: the reply is complete |

Why the marker: a reply longer than 4096 characters arrives as several response packets, and nothing says which
one is the last. But the server answers requests strictly in order, so the client sends a second, meaningless
request (type 0 is not valid from a client) after the first fragment: when its answer arrives, every fragment
of the real reply has arrived before it. It is sent only after the first fragment, because vanilla drops a
connection that delivers two requests in one read. Some servers also send an empty response packet before the
login answer; the client skips it. `stop` reads exactly one packet (the server may close the connection
afterwards), so it sends no marker.

TCP is a byte stream: one packet may arrive in several `read`s, or several packets in one. So the client always
reads "exactly 4 bytes", then "exactly *length* bytes" (`read_exact` in `rcon.rs`), however the network splits
them.

## The files, one by one

### main.rs: the entry point, and `enum`s for the subcommands

```rust
match args::parse(&prog, &rest) {
    Parsed::Help(text) => { print!("{text}"); ExitCode::SUCCESS }
    Parsed::Error(text) => { eprint!("{text}"); ExitCode::from(2) }
    Parsed::Run(command) => match run(&command) { ... },
}
```

`Parsed` and `Command` (in `args.rs`) are **enums**: a value is exactly one of the variants, and each variant
carries its own data.

```rust
pub enum Command {
    Cmd { words: Vec<PyStr> },
    Stop,
    Countdown { seconds: PyInt },
    Worldborder { dimension: PyStr, radius: PyInt },
    Gamerule { name: PyStr, value: PyStr, dimensions: Vec<PyStr> },
}
```

Python's argparse gives you one `Namespace` where `args.radius` exists or not depending on the subcommand, and a
typo is an `AttributeError` at run time. In Rust, `Command::Cmd` has no `radius`, so asking for it doesn't compile,
and `match` must handle every variant: forget `Gamerule` and the compiler refuses the program. The closest Python
is a `match` statement over dataclasses, with a type checker that insists on exhaustiveness.

### error.rs: `Result` and `?` instead of exceptions

Python raises; the exception flies up until an `except` catches it, and if none does, Python prints a traceback
and exits with 1. Rust has no exceptions: a function that can fail returns `Result<T, E>`, which is either
`Ok(value)` or `Err(error)`, and the caller must deal with both. `?` after an expression means "if it is an `Err`,
return it from this function right now":

```rust
pub fn cmd(words: &[PyStr]) -> Result<String, PyError> {
    let mut r = connect()?;                       // an error here returns from cmd()
    r.command(&PyStr::join(words, " "), None, false)
}
```

is the Rust spelling of

```python
def cmd(words):
    r = connect()  # an exception here leaves cmd()
    return r.command(" ".join(words))
```

The error type, `PyError`, is an enum with one variant per Python exception the script can end with:
`Rcon(String)` for `RconError`, `Os { errno, strerror, filename }` for `OSError` and its subclasses, `Timeout`,
`Key(...)` for `KeyError`, and so on. `impl fmt::Display for PyError` is its `__str__`; `is_caught()` answers "would
`except (OSError, RconError)` catch this?", which is what `stop` and `countdown` do in Python:

```rust
match rcon_part() {
    Ok(()) => {}
    Err(e) if e.is_caught() => {
        eprintln!("RCON stop failed ({e}); systemd will fall back to SIGTERM");
        return Ok(1);
    }
    Err(e) => return Err(e),      // not caught in Python either: up to main()
}
```

Anything that reaches `main` is printed the way Python's traceback ends (`traceback_line()`:
`ConnectionRefusedError: [Errno 111] Connection refused`) with exit status 1.

### rcon.rs: `struct`, `impl` blocks, ownership and borrowing

A Python class holds data and methods together. Rust splits them: a `struct` declares the data, an `impl` block
adds the methods.

```rust
pub struct Rcon {
    stream: TcpStream,
    timeout: Duration,
    last_id: i32,
}

impl Rcon {
    pub fn connect(host: &str, port: u16, password: &PyStr, timeout: Duration) -> Result<Rcon, PyError> { ... }
    fn send(&mut self, kind: i32, payload: &PyStr) -> Result<i32, PyError> { ... }
    pub fn command(&mut self, cmd: &PyStr, timeout: Option<f64>, single: bool) -> Result<String, PyError> { ... }
}
```

`connect` has no `self`: it is a constructor, Python's `__init__` plus `__enter__` (it returns a connected,
logged-in `Rcon` or an error, never a half-built object). `&mut self` says "this method changes the object"
(it writes to the socket, bumps the id); a method with plain `&self` couldn't. Python's `self` is always mutable;
Rust makes you say it, and then guarantees nobody else touches the object meanwhile.

**Ownership and borrowing**, in the packet reader:

```rust
fn read_exact(&mut self, n: usize) -> Result<Vec<u8>, PyError> {
    let mut buf = Vec::new();
    let mut chunk = [0u8; 8192];
    while buf.len() < n {
        let want = (n - buf.len()).min(chunk.len());
        match self.stream.read(&mut chunk[..want]) {
            Ok(0) => return Err(PyError::Rcon("connection closed by server".into())),
            Ok(got) => buf.extend_from_slice(&chunk[..got]),
            Err(e) if e.kind() == ErrorKind::Interrupted => {}
            Err(e) => return Err(PyError::from_io(&e)),
        }
    }
    Ok(buf)
}
```

- Every value has exactly one **owner**. `buf` belongs to this function until `Ok(buf)` *moves* it to the caller;
  after that, this function can't use it. No copy is made and nothing is garbage-collected: when the last owner
  goes away, the memory is freed at that moment. (In Python both names would point at the same list, and the
  list lives until the last reference dies.)
- `&mut chunk[..want]` **lends** the first `want` bytes of `chunk` to `read`, which may write into them; the loan
  ends when `read` returns, and `chunk` is ours again. `&chunk[..got]` lends them read-only to
  `extend_from_slice`, which copies them into `buf`. A *slice* (`&x[a..b]`) is a view, not a copy: Python's
  `memoryview(chunk)[:want]`, not `chunk[:want]`.
- The compiler checks these rules (the "borrow checker"): no one can read `chunk` while `read` is writing it, and
  no loan can outlive what it borrows. Data races and use-after-free don't compile.

`Ok(got)`, `Ok(0)`, `Err(e) if ...` are **patterns**: `match` tries them top to bottom, like Python's
`match`/`case`, and binds the names inside.

Decoding a received packet mirrors `_recv` step by step, including Python's slicing rule that `data[8:-2]` is
empty for a 9-byte packet:

```rust
let body = if data.len() >= 10 { &data[8..data.len() - 2] } else { &[][..] };
Ok(Packet { id, kind, body: String::from_utf8_lossy(body).into_owned() })
```

`String::from_utf8_lossy` is `bytes.decode("utf-8", errors="replace")` (the same replacement rule: one U+FFFD per
invalid sequence).

### properties.rs: careful file reading and a hand-written parser

`read_untrusted` opens server.properties the way `mc_rcon.read_untrusted` does: `O_NOFOLLOW` (never through a
symlink), `O_NONBLOCK` (a FIFO can't hang us), refuse anything but a regular file, read at most 1 MiB. Rust's
`OpenOptions` builder is the `os.open(path, flags)` call:

```rust
let file = OpenOptions::new()
    .read(true)
    .custom_flags(O_NONBLOCK | O_NOFOLLOW | O_NOCTTY)
    .open(&os_path)
    .map_err(|e| PyError::from_io_with_path(&e, Some(path)))?;
```

`.map_err(|e| ...)` converts the error type (here `io::Error` into our `PyError`, with the path, as Python's
`OSError` carries it) so that `?` can pass it on. `|e| ...` is a *closure*, Python's `lambda e: ...`.

Python parses each line with a regular expression; Rust's std has no regex engine, so `split_key_value` walks the
characters doing exactly what that pattern does (key characters, escapes, then `\s*[=:\s]\s*`). The Rust unit
tests next to it are the Python test's example plus every edge case the differential test found.

### pystr.rs: Python's text rules

A Rust `String` is always valid UTF-8; a Python `str` can hold "lone surrogates" (Python decodes a non-UTF-8
argument into them; a `\uD83D` escape in server.properties makes one), and fails only when such a string is
encoded. To fail at the same moment with the same message, `PyStr` stores code points as `u32` numbers:

```rust
#[derive(Clone, Debug, Default, PartialEq, Eq, Hash)]
pub struct PyStr(Vec<u32>);
```

`#[derive(...)]` writes the boring methods for you: `PartialEq` is `__eq__`, `Hash` is `__hash__` (so a `PyStr` can
be a `HashMap` key, as a `str` is a dict key), `Debug` is a `__repr__` for tests. The same file holds Python's
`str.isspace()` (which differs from Rust's `char::is_whitespace` by four characters), `str.splitlines()`, `repr()`,
and `int()` (`PyInt`: Python ints have no size limit, so the number is kept as its digits).

### args.rs: argparse's rules

`argparse` accepts more than most people know: `--rad 5` for `--radius 5`, `--radius=5`, `-5` as a value but `-x`
as an unknown option, `--` to end options, `-h` anywhere. A drop-in must accept and refuse the same command lines
with the same `error:` line and exit status 2, so this file is a small copy of argparse's own algorithm
(`_parse_known_args` in Python 3.13.5, the spec), limited to the options `mc_rcon.py` defines. It is the most intricate file;
read it last. The usage and help texts are argparse-like, not identical (nothing parses them).

### ops.rs: the operations

One function per `mc_rcon.py` function, in the same order of steps: `connect`, `online_players`, `stop`,
`countdown`, `worldborder`, `gamerule`. Three things std can't do, and what replaces them:

- **No `kill(pid, 0)`** (std has no signals API, and `unsafe` calls into libc are forbidden here): `pid_alive`
  asks whether `/proc/<pid>` exists, which is the same answer for the server's own user, the only one that runs
  `stop`.
- **No TLS**: countdown's Discord note (HTTPS) is posted by the same Python code as before, `notify.py
  restart-alert SECONDS ONLINE`, run next to the binary, and only when a webhook is configured.
- **No regular expressions**: each pattern (`There are (\d+)`, `(\d+(?:\.\d+)?) block`, the game-rule reply) is a
  small hand-written search with a comment showing the pattern it replaces.

Closures that return `Result` stand in for `try:` blocks:

```rust
let result = (|| -> Result<(), PyError> {
    let mut r = connect()?;
    ...
    Ok(())
})();
match result {
    Err(e) if e.is_caught() => { say(&format!("countdown skipped: {e}")); Ok(()) }
    other => other,
}
```

### Tests

Unit tests live in each file under `#[cfg(test)] mod tests` (compiled only for `cargo test`); each `#[test]`
function is a pytest test function, `assert_eq!(a, b)` is `assert a == b` with both values printed on failure.
`rcon.rs` starts a tiny scripted server on a thread to test fragmented reads (it writes replies one byte at a
time), multi-packet replies, wrong passwords, timeouts and refused connections.

## The stop path

`stop` is `ExecStop`: systemd runs it to stop the server, and a wrong stop could lose world data. It does what
Python did: connect, `save-all flush` (writes every chunk; most of the 110 s budget), `stop` (one packet), then
wait for `$MAINPID` to exit. The unit runs:

```text
/bin/sh -c 'smallworld-rcon stop; rc=$?; [ "$rc" -le 1 ] && exit "$rc"; echo "... falling back ..." >&2; exec mc_rcon.py stop'
```

- **0** (stopped) and **1** (RCON failed, or the JVM outlived the budget) are final. On 1 systemd sends SIGTERM,
  and the server's shutdown hook still saves the world. Running `mc_rcon.py stop` after a 1 would repeat
  `save-all flush` and `stop` with a fresh 110 s budget, past `TimeoutStopSec=120`: systemd would kill it halfway.
  The two implementations reach the same verdict for the same server (that is what the differential tests prove),
  so the second attempt would add nothing but risk.
- **Anything else** means the binary gave no verdict: missing (127), not executable or the wrong architecture
  (126), killed or crashed (a panic aborts: 134), a usage error (2, impossible with a fixed command line). Then
  `mc_rcon.py stop` runs, from the start, with the full budget. Nothing was sent to the server in those cases
  (or the binary died mid-way, and a repeated `save-all flush`/`stop` is harmless: `stop` on a stopping server is a
  no-op).

`tests/test_rust_rcon.py` runs this exact shell line from the unit template with fake binaries for each case;
`tests/e2e/deploy_check.py` restarts the real server and checks that `ExecStop` exited 0 without a fallback.

## What "identical" means, and how it is proved

`tests/test_rust_rcon.py` runs the Python script and the committed binary side by side, each against its own fake
RCON server that records every packet, and asserts the same exit status, stdout, stderr and packets. For an
exception Python doesn't catch, the binary prints exactly the lines Python's traceback ends with. Cases: success,
long and fragmented replies, broken packets, wrong password, connection refused, RCON disabled, a missing or odd
server.properties (escapes, separators, comments, Unicode whitespace, invalid UTF-8, a symlink, a FIFO, a
directory), login and command timeouts (the real 10 s), countdown marks and its Discord note, world border and game
rule idempotency (two runs: `CHANGED` then `OK`, same packets), stop with `$MAINPID`, non-UTF-8 arguments, and about
80 argparse command lines.

Known differences from the spec Python, all outside anything the product or a person types:

- Python's `int()` and `\d` also accept non-ASCII decimal digits (`٣`); the binary accepts ASCII digits only.
- `repr()` of unassigned or rare "format" Unicode characters may show them raw where Python escapes them.
- `rcon.ip` with non-ASCII characters (Python's IDNA encoding), name-resolution error codes other than "not
  known" and "temporary failure", ports of 2^63 and above.
- A countdown of about 292 years (Python fails its sleep with `EINVAL`); the countdown skips spawning notify.py
  when no Discord webhook is configured (Python would still import alerts.py and notify.py).
- Usage and help texts are argparse-like, not byte for byte; the `error:` line and exit status 2 are identical.

## Which Python is the spec

`mc_rcon.py` itself doesn't behave the same on every Python: argparse changed between patch releases (newer ones
treat `-hX`, `-=x` and a `--` before the subcommand differently; the list of choices in an error is quoted in some
releases and not in others), and CPython rewords some messages (`time.sleep`'s overflow, `os.kill`'s in 3.14). A
drop-in has to copy one of them, so the binary's spec is **CPython 3.13.5, Debian 13's `python3`**: the Python of
the e2e image and of a fresh server (the roles also allow Debian 12, Python 3.11.2, and Ubuntu 24.04,
3.12.3). On that Python, `tests/test_rust_rcon.py` demands exact parity (CI's `rust` job runs it there).

On the other Pythons a server may run (3.11, 3.12, other 3.13.x) the same tests demand parity except for the
documented, version-dependent behaviours below, where that Python's alternative is accepted from `mc_rcon.py`
and the binary must still give the spec's answer (`KNOWN` and `ARGPARSE_KNOWN` in the test). On Pythons no
server runs (3.14 and later) the side-by-side tests are skipped with that reason; the rest of the file (no
dependencies, pins, the `ExecStop` fallback) still runs.

| Behaviour | Spec (3.13.5) and the binary | Elsewhere |
|---|---|---|
| `time.sleep` past ~292 years (`countdown 99999999999`) | `OverflowError: timestamp too large to convert to C PyTime_t` | 3.11, 3.12: `... C _PyTime_t`; some 3.13 builds: `timestamp out of range for C PyTime_t` |
| argparse's invalid-choice list | `(choose from cmd, stop, ...)` | quoted (`'cmd', 'stop', ...`) in 3.11, 3.12 before 3.12.8, 3.13 before 3.13.3 and from 3.13.14 |
| `cmd -hello`, `stop -hfoo` (`-h` with letters glued on) | help, exit 0 | older patch releases (Debian 12's 3.11.2, 3.12.0): `argument -h/--help: ignored explicit argument 'ello'`, exit 2 |
| `cmd -=x`, `worldborder -=x` | `ambiguous option: -=x could match -h, --help...` | older patch releases (3.11.x, 3.12.0, 3.13.0): `the following arguments are required: ...` |
| `-- cmd list` (a `--` before the subcommand) | the `--` is dropped, `cmd list` runs | older patch releases (3.11.x, 3.12.0, 3.13.0): `invalid choice: '--'` |
| `os.kill` past a C int (`MAINPID=99999999999`) | `signed integer is greater than maximum` | 3.14: `Python int too large to convert to C int` (not a server Python) |
| `cmd -1x`, `cmd -5.` | unknown option: `the following arguments are required: command` | 3.14: taken as a word (not a server Python) |

**No production call site reaches any of them.** `ExecStop` runs `smallworld-rcon stop` with systemd's real
`$MAINPID`; the deploy runs `countdown 60` (`mc_restart_countdown_seconds`), `worldborder --dimension D --radius R`
and `gamerule --name N --value V --dimension D ...` with values from the schema-checked `server.yml`; the `mc`
helper always starts with `cmd`, so a `--` can never come before the subcommand and its only exposure is a person
typing nonsense: `mc -=x` fails with exit status 2 and sends nothing on every Python (only the wording of the
`error:` line differs), and `mc -hello` prints help where Debian 12's Python refused it with status 2; neither
sends anything to the server. So the binary behaves exactly as `mc_rcon.py` did on any supported server for
everything the product and its admin use.

## Build, test and run it yourself

Install Rust with [rustup](https://rustup.rs). In `rust/`, rustup reads `rust-toolchain.toml` and installs the
pinned compiler, clippy, rustfmt and both musl targets the first time you run cargo there.

```bash
cd rust
cargo test                      # the unit tests
cargo clippy --all-targets -- -D warnings
cargo fmt                       # format (CI runs `cargo fmt --check`)
cargo build                     # debug build: target/debug/smallworld-rcon

# run it against a server: a directory with a server.properties (enable-rcon, rcon.port, rcon.password)
MC_SERVER_DIR=/path/to/server cargo run -- cmd list
cargo run -- --help

# the differential tests against mc_rcon.py, with your build instead of the committed binary
cd .. && SMALLWORLD_RCON_BIN=rust/target/debug/smallworld-rcon pytest -q tests/test_rust_rcon.py

# after changing the source: rebuild the committed binaries, then pin the printed sha256s in group_vars
python scripts/build_rust.py build
python scripts/build_rust.py check   # what CI runs: a rebuild elsewhere must give the same bytes
```

To try it against a real server, point `MC_SERVER_DIR` at any server directory with RCON enabled.

## Exercises

Three small, safe changes, each with a hint and the test to write first. None of them is shipped: they are for
practice on a branch.

### 1. A `--json` flag for `cmd`

`smallworld-rcon cmd --json list` prints `{"command": "list", "reply": "There are ..."}`.

- Hint: add `Opt::Json` to `args.rs` (an option without a value, like `Opt::Help`, registered only for the `cmd`
  parser), a `json: bool` field on `Command::Cmd`, and in `main.rs` print either the plain reply or the JSON.
  std has no JSON library: write a tiny `fn json_string(s: &str) -> String` that escapes `"`, `\` and control
  characters.
- Test first: in `args.rs`, `assert_eq!(outcome("cmd --json list"), ...)` with the new field; in `main.rs` or a new
  module, unit tests for `json_string` (`"a\"b"` becomes `"\"a\\\"b\""`). This deliberately breaks "identical to
  mc_rcon.py" for that flag only, so the differential test needs no change (it never passes `--json`).

### 2. A `players` subcommand

`smallworld-rcon players` prints just the number of players online (`online_players` already computes it).

- Hint: a new `Command::Players` variant, `"players"` in `SUBCOMMANDS` and a `Spec` for it in `spec_for` (copy
  `stop`'s), and a `match` arm in `main.rs`'s `run`. The compiler will point at every `match` you forgot.
- Test first: `assert_eq!(outcome("players"), "Players")` in `args.rs`; then a test in `rcon.rs` style that serves
  `There are 3 of a max of 20 players online` and checks `online_players` returns 3.

### 3. A friendlier refused-connection message

Today a refused connection prints Python's `ConnectionRefusedError: [Errno 111] Connection refused`. Try printing a
hint afterwards (`is the server running? systemctl status minecraft`), only in `cmd`.

- Hint: in `main.rs`, match `Err(PyError::Os { errno: 111, .. })` for `Command::Cmd` before the generic arm, print
  the usual line, then the hint. `..` in a pattern means "and any other fields".
- Test: the `rcon.rs` test `closed_connection_and_refused` shows how to get a refused port; then run
  `pytest tests/test_rust_rcon.py` and watch the differential test fail on exactly that case: it is the safety net
  that keeps the binary a drop-in. Decide whether the hint is worth that (in the product, it isn't: scripts read
  that output).
