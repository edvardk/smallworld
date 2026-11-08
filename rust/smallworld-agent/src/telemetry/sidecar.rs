//! `smallworld-agent telemetry`: mc_agent.py's `telemetry` subcommand (`telemetry_main`, `SidecarTelemetry`,
//! `overhead_loop`, `overhead_round`), the telemetry alone in a process of its own. The product doesn't install a
//! unit for it in rust mode (the agent runs telemetry in-process, `daemon::run`); it exists for parity with
//! `mc_agent.py telemetry`, which the telemetry parity harness (tests/telemetry_parity) drives black-box, so the
//! module the daemon runs in-process is proven against the same goldens. What only this entry point does, as in
//! Python:
//!
//! - after every poll (a failed one too) it writes `/var/lib/mc-telemetry/online.json`, `{uuid: {u, n, tips,
//!   join}}` (sorted keys, 0640);
//! - it reads systemd's end-of-run lines itself (`journalctl ... SYSLOG_IDENTIFIER=systemd`, cursor in
//!   `/var/lib/mc-telemetry/journal.json`, every `LOG_BATCH_S`), where the daemon's logs loop feeds them;
//! - the overhead card also measures `mc-telemetry.service` (`SIDECAR_UNITS`).

use super::json::{J, OMap};
use super::overhead::{OVERHEAD_UNITS, unit_stats};
use super::{PyExc, Telemetry, atomic_text, make_telemetry_with};
use crate::misc::warnings::WARN;
use crate::settings::{Settings, process_env};
use crate::ship::ShipError;
use crate::ship::archive::LogState;
use crate::ship::journal::{
    LOG_BATCH_MAX_BYTES, LOG_BATCH_MAX_ENTRIES, LOG_CATCHUP_BATCHES, Slicer, journal_argv,
    read_journal,
};
use crate::types::VERSION;
use std::path::Path;
use std::sync::Arc;

pub const ONLINE_FILE: &str = "/var/lib/mc-telemetry/online.json";
pub const SIDECAR_JOURNAL_STATE: &str = "/var/lib/mc-telemetry/journal.json";
pub const SIDECAR_UNITS: [&str; 3] = [OVERHEAD_UNITS[0], OVERHEAD_UNITS[1], "mc-telemetry.service"];
pub const SIDECAR_JOURNAL_MATCH: &str = "SYSLOG_IDENTIFIER=systemd";
pub const ONLINE_KEYS: [&str; 4] = ["u", "n", "tips", "join"];

/// mc_agent.py `online_map(online)`: each player narrowed to `ONLINE_KEYS`.
pub fn online_map(online: &OMap) -> OMap {
    let mut out = OMap::new();
    for (u, p) in online.iter() {
        let mut m = OMap::new();
        for k in ONLINE_KEYS {
            m.insert(
                k,
                p.as_obj()
                    .and_then(|p| p.get(k))
                    .cloned()
                    .unwrap_or(J::Null),
            );
        }
        out.insert(u.as_str(), m);
    }
    out
}

/// mc_agent.py `write_online(path, online)`: `json.dumps(..., sort_keys=True)`, atomic, 0640.
///
/// # Errors
/// The `OSError`.
pub fn write_online(path: &Path, online: &OMap) -> std::io::Result<()> {
    let text = smallworld_core::pyjson::dumps_sorted(&J::Obj(online_map(online)).to_value());
    atomic_text(path, &text)
}

/// `SidecarTelemetry.run`: `Telemetry.run` whose every poll ends by publishing the online map.
fn run(t: &Telemetry) {
    t.run_with(
        &|| match write_online(Path::new(ONLINE_FILE), &t.online_map()) {
            Err(e) => {
                let x = PyExc::from_io(&e, None);
                WARN.fail("online.json", &format!("{}: {}", x.class, x.text), false);
            }
            Ok(()) => WARN.ok("online.json"),
        },
    );
}

/// mc_agent.py `overhead_round(state, observe)`: one batch of systemd's own journal lines; true if more wait.
fn overhead_round(state: &Path, t: &Telemetry) -> Result<bool, ShipError> {
    let mut st = LogState::load(state);
    let mut argv = journal_argv(&st.cursor);
    argv.push(SIDECAR_JOURNAL_MATCH.to_string());
    let mut slicer = Slicer::default();
    let batch = read_journal(
        &st.cursor,
        LOG_BATCH_MAX_ENTRIES,
        LOG_BATCH_MAX_BYTES,
        Some(&argv),
        &mut || slicer.cede(),
    )?;
    let entries: Vec<crate::types::Obj> = batch
        .entries
        .iter()
        .map(crate::ship::journal::Entry::to_obj)
        .collect();
    t.overhead.observe_journal(&entries);
    if batch.cursor != st.cursor {
        st.cursor = batch.cursor;
        st.save(state)?;
    }
    Ok(batch.full)
}

/// mc_agent.py `overhead_loop(s, t, stop, state)`.
fn overhead_loop(s: &Settings, t: &Telemetry) {
    #[allow(clippy::cast_precision_loss)]
    let wait = s.log_batch_s as f64;
    let state = Path::new(SIDECAR_JOURNAL_STATE);
    while !t.stop.wait(wait) {
        let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            for _ in 0..LOG_CATCHUP_BATCHES {
                if !overhead_round(state, t)? || t.stop.is_set() {
                    break;
                }
            }
            Ok::<(), ShipError>(())
        }));
        match outcome {
            Ok(Ok(())) => WARN.ok("overhead journal"),
            Ok(Err(ShipError::Ingest(e))) => {
                WARN.fail("overhead journal", &format!("IngestError: {e}"), false);
            }
            Ok(Err(ShipError::Agent(e))) => {
                let x = PyExc::from(e);
                WARN.fail(
                    "overhead journal",
                    &format!("{}: {}", x.class, x.text),
                    false,
                );
            }
            Err(p) => WARN.fail(
                "overhead journal",
                &format!("panic: {}", crate::daemon::panic_text(&*p)),
                false,
            ),
        }
    }
}

/// mc_agent.py `telemetry_main()`: exit 2 (one line on stderr) for bad settings or EXPERIENCE off.
pub fn main() -> u8 {
    let env = process_env();
    let settings = match Settings::from_env(&env) {
        Ok(s) if s.experience => s,
        Ok(_) => {
            eprintln!("mc-telemetry: EXPERIENCE is off: the telemetry sidecar has nothing to do");
            return 2;
        }
        Err(e) => {
            eprintln!("mc-telemetry: {e}");
            return 2;
        }
    };
    let link = crate::link::Link::new(settings.clone());
    let t = match make_telemetry_with(&settings, &env, link, || unit_stats(&SIDECAR_UNITS)) {
        Ok(t) => Arc::new(t),
        Err(e) => {
            eprintln!("mc-telemetry: {e}");
            return 2;
        }
    };
    if let Err(e) = std::fs::create_dir_all(&settings.state_dir) {
        let x = PyExc::from_io(&e, Some(&settings.state_dir));
        eprintln!("Traceback (most recent call last):");
        eprintln!("  File \"mc_agent.py\", in telemetry_main");
        eprintln!("{}: {}", x.class, x.text);
        return 1;
    }
    let stop = Arc::clone(&t.stop);
    crate::sys::handle_signals(&crate::sys::DAEMON_SIGNALS, move |_| stop.set());
    let host = crate::settings::url_hostname(&settings.ingest_url).unwrap_or_else(|| "None".into());
    println!("mc-telemetry {VERSION}: pushing to {host}");
    let spawned = [
        {
            let t = Arc::clone(&t);
            std::thread::Builder::new()
                .name("telemetry".into())
                .spawn(move || run(&t))
        },
        {
            let t = Arc::clone(&t);
            let s = settings.clone();
            std::thread::Builder::new()
                .name("overhead".into())
                .spawn(move || overhead_loop(&s, &t))
        },
    ];
    for th in &spawned {
        if let Err(e) = th {
            eprintln!("mc-telemetry: can't start a thread: {e}");
            return 1;
        }
    }
    while !t.stop.wait(3600.0) {}
    0
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn online_map_narrows_like_python() {
        let online = OMap::new().with(
            "u1",
            OMap::new()
                .with("u", "u1")
                .with("n", "Bob")
                .with("ping", 4_i64)
                .with("tips", true)
                .with("join", 5_i64),
        );
        let text = smallworld_core::pyjson::dumps_sorted(&J::Obj(online_map(&online)).to_value());
        assert_eq!(
            text,
            r#"{"u1": {"join": 5, "n": "Bob", "tips": true, "u": "u1"}}"#
        );
    }
}
