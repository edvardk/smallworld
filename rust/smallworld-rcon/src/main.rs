//! smallworld-rcon: a drop-in for the command line of Smallworld's `scripts/mc_rcon.py`.
//!
//! ```text
//! smallworld-rcon cmd list                      # run a command, print the reply
//! smallworld-rcon stop                          # save-all flush, stop, wait for $MAINPID (systemd ExecStop)
//! smallworld-rcon countdown 60                  # announce a restart at 60/30/10s (skipped if nobody online)
//! smallworld-rcon worldborder --dimension minecraft:overworld --radius 5000   # idempotent
//! smallworld-rcon gamerule --name N --value V --dimension D [--dimension D ...]  # idempotent
//! ```
//!
//! Same arguments, output, exit status, timeouts and packets as the Python script (tests/test_rust_rcon.py runs
//! both side by side). The walkthrough for Python programmers is docs/rust/smallworld-rcon.md.

// No `unsafe` anywhere in this crate (Cargo.toml's [lints] says the same, this makes it impossible to override).
#![forbid(unsafe_code)]

// Each `mod` line pulls in the file of that name (src/args.rs, ...), like a Python import of a sibling module.
mod args;
mod errno;
mod error;
mod ops;
mod properties;
mod pystr;
mod rcon;

use args::{Command, Parsed};
use error::PyError;
use pystr::PyStr;
use std::os::unix::ffi::OsStrExt;
use std::process::ExitCode;

/// Runs one parsed command: `main()`'s `if args.cmd == ...` chain in mc_rcon.py. Returns the exit status.
fn run(command: &Command) -> Result<u8, PyError> {
    match command {
        Command::Cmd { words } => {
            ops::say(&ops::cmd(words)?);
            Ok(0)
        }
        Command::Stop => {
            let pid = ops::main_pid()?;
            let status = ops::stop(pid.as_ref())?;
            Ok(u8::try_from(status).unwrap_or(1))
        }
        Command::Countdown { seconds } => {
            ops::countdown(seconds)?;
            Ok(0)
        }
        Command::Gamerule {
            name,
            value,
            dimensions,
        } => {
            let changed = ops::gamerule(name, value, dimensions)?;
            ops::say(if changed { "CHANGED" } else { "OK" });
            Ok(0)
        }
        Command::Worldborder { dimension, radius } => {
            let changed = ops::worldborder(dimension, radius)?;
            ops::say(if changed { "CHANGED" } else { "OK" });
            Ok(0)
        }
    }
}

fn main() -> ExitCode {
    // The arguments as Python would decode them (invalid UTF-8 becomes lone surrogates, see pystr.rs).
    let mut argv = std::env::args_os().map(|a| PyStr::from_os_bytes(a.as_bytes()));
    // argparse names the program after the base name of argv[0].
    let prog = argv
        .next()
        .map(|a| {
            a.display()
                .rsplit('/')
                .next()
                .unwrap_or_default()
                .to_string()
        })
        .filter(|p| !p.is_empty())
        .unwrap_or_else(|| "smallworld-rcon".to_string());
    let rest: Vec<PyStr> = argv.collect();

    match args::parse(&prog, &rest) {
        Parsed::Help(text) => {
            print!("{text}");
            ExitCode::SUCCESS
        }
        Parsed::Error(text) => {
            eprint!("{text}");
            ExitCode::from(2)
        }
        Parsed::Run(command) => match run(&command) {
            Ok(status) => ExitCode::from(status),
            // What Python does with an exception nothing caught: the traceback (its last line here), status 1.
            Err(PyError::HelperFailed) => ExitCode::from(1),
            Err(e) => {
                eprintln!("{}", e.traceback_line());
                ExitCode::from(1)
            }
        },
    }
}
