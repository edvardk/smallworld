//! The daemon (`mc_agent.py` with no arguments: `main()` + `Agent`; 2275-2306, 2394-2405, 2568-2585, 2768-2785).
//! Owner: **link** (wiring); each loop comes from its module: metrics/inventory (data), logs (ship), control
//! (link), panel sync (sync), self-test (misc), client codes (data).
//!
//! Until every module is ported the daemon refuses to start: running it half ported would answer panel commands
//! with refusals the Python agent doesn't give. Test-only escape hatch: `SMALLWORLD_AGENT_ALLOW_STUBS=1` starts it
//! anyway, in **debug builds only** (`cfg!(debug_assertions)`; ignored by the release profiles), so the parity
//! scenarios of one module can run before the others land. A stubbed loop then logs that it isn't ported and ends.
//!
//! Every loop thread is guarded: a loop that panics or returns an error is logged and started again after 5 s
//! (Python's loops can't die, each round catches `Exception`; this is the net under a round that doesn't).
//! Loops wrap their own rounds with `round` (Python's `except Exception as e: print(f"{what}: {type}: {e}")`).

use crate::actions::{Executor, Panel};
use crate::jobs::{Event, Jobs};
use crate::link::Link;
use crate::link::verify::Nonces;
use crate::link::ws::WsHandle;
use crate::misc::Misc;
use crate::misc::warnings::{WARN, Warnings};
use crate::settings::{Settings, process_env};
use crate::types::{AResult, AgentError, VERSION, Value};
use serde_json::json;
use smallworld_core::paths;
use std::panic::{AssertUnwindSafe, catch_unwind};
use std::path::PathBuf;
use std::sync::atomic::AtomicBool;
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};

/// Modules still stubs (each owner removes its name when ported; `main` refuses to start while any remain).
pub const NOT_PORTED: [&str; 1] = ["ship"];

/// The test-only switch (see the module comment).
pub const ALLOW_STUBS_ENV: &str = "SMALLWORLD_AGENT_ALLOW_STUBS";

pub fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(PoisonError::into_inner)
}

/// The daemon's shared state (`Agent`). Loops get `&Daemon`; a module that needs more daemon-wide state adds a
/// field here (built in `Daemon::new`) in its own small commit.
pub struct Daemon {
    pub settings: Settings,
    pub stop: Arc<Event>,
    /// The Worker's HTTP side.
    pub link: Link,
    /// `Agent.inventory_due`: a state change pushes the inventory now.
    pub inventory_due: Arc<Event>,
    /// mc_agent.py's module-level `WARN`.
    pub warn: &'static Warnings,
    /// `Agent.ws_connected` (the metrics sample's `ws`).
    pub ws_connected: AtomicBool,
    /// `Agent.ws_established`: the last session got as far as the upgrade.
    pub ws_established: AtomicBool,
    /// `Agent.ws`: the live session's sender, `None` while polling (the console tail checks it).
    pub ws: Mutex<Option<Arc<WsHandle>>>,
    /// `Agent.nonces` (used by the control thread only).
    pub nonces: Mutex<Nonces>,
    /// `Agent.panel`.
    pub panel: Arc<Panel>,
    /// `Agent.executor` (used by the control thread only).
    pub executor: Mutex<Executor>,
    /// The self-test, the Mojang lookup and the product version this process started with (misc).
    pub misc: Arc<Misc>,
}

/// `Agent.report_job(job)`: `POST /ingest/job` (failures only logged: the inventory carries the job too), then
/// the inventory is due.
pub fn report_job(link: &Link, warn: &Warnings, inventory_due: &Event, job: &Value) {
    match link.post_json("/ingest/job", &json!({"agent": VERSION, "job": job})) {
        Err(e) => warn.fail("job", &e.to_string(), false),
        Ok(_) => warn.ok("job"),
    }
    inventory_due.set();
}

impl Daemon {
    /// `Agent(s)`: loads the nonces (fail closed, audited), builds the panel (background job runner on
    /// `JOB_LOCK_FILE`, job reports to the Worker) and the executor. Starts no thread.
    ///
    /// # Errors
    /// The `ValueError` text of a bad GITHUB_API_URL or MOJANG_LOOKUP_URLS (Python's `Agent(s)` raises it).
    pub fn new(settings: Settings) -> Result<Self, String> {
        let stop = Arc::new(Event::default());
        let link = Link::new(settings.clone());
        let inventory_due = Arc::new(Event::default());
        let warn: &'static Warnings = &WARN;
        let nonces = Nonces::new(
            Some(settings.state_dir.join("nonces.json")),
            crate::sys::now(),
        );
        let report: crate::jobs::Report = {
            let (link, due) = (link.clone(), Arc::clone(&inventory_due));
            Arc::new(move |job: &Value| report_job(&link, warn, &due, job))
        };
        let jobs = Jobs::new(report, Some(PathBuf::from(paths::JOB_LOCK_FILE)), true);
        let mut panel = Panel::new(jobs, &settings.state_dir);
        panel.changed = {
            let due = Arc::clone(&inventory_due);
            Arc::new(move || due.set())
        };
        // Agent.github, Agent.selftest_state, Lookup(lookup_urls()), product_started (misc)
        let misc = Arc::new(Misc::new(
            &process_env(),
            &settings.state_dir,
            crate::misc::github_token_fetch(link.clone()),
        )?);
        misc.wire(&mut panel);
        let panel = Arc::new(panel);
        // `Executor(lambda: collect(Counters()), self.tail.request, self.panel, self.telemetry)`: data wires the
        // console tail (`ConsoleTail.request`, which needs `Daemon::ws`); the telemetry view (actions') is below.
        let sample = crate::data::status_sample();
        // `self.telemetry = self.make_telemetry() if s.experience else None`: in rust mode telemetry is the
        // Python sidecar; the agent keeps `Messages` and reads its online.json (actions/telemetry.rs). A bad
        // MESSAGE_* number is `int()`'s ValueError, as in Python.
        let telemetry: Option<Arc<dyn crate::actions::Telemetry>> = if settings.experience {
            Some(Arc::new(crate::actions::telemetry::Sidecar::from_env(
                &process_env(),
            )?))
        } else {
            None
        };
        let executor = Executor::new(sample, None, Some(Arc::clone(&panel)), telemetry);
        Ok(Self {
            settings,
            stop,
            link,
            inventory_due,
            warn,
            ws_connected: AtomicBool::new(false),
            ws_established: AtomicBool::new(false),
            ws: Mutex::new(None),
            nonces: Mutex::new(nonces),
            panel,
            executor: Mutex::new(executor),
            misc,
        })
    }

    /// `Agent.ws` for the console tail: the live session's sender, if any.
    pub fn ws_sender(&self) -> Option<Arc<WsHandle>> {
        lock(&self.ws).clone()
    }
}

/// A panic payload's text.
pub fn panic_text(p: &(dyn std::any::Any + Send)) -> String {
    p.downcast_ref::<&str>()
        .map(ToString::to_string)
        .or_else(|| p.downcast_ref::<String>().cloned())
        .unwrap_or_else(|| "panic".into())
}

/// One loop round the way Python's loops guard theirs: `try: ... except Exception as e: print(f"{what}:
/// {type(e).__name__}: {e}", file=sys.stderr)`. A panic counts as an exception (`panic: <text>`). `None` when the
/// round failed.
pub fn round<T>(what: &str, f: impl FnOnce() -> AResult<T>) -> Option<T> {
    match catch_unwind(AssertUnwindSafe(f)) {
        Ok(Ok(v)) => Some(v),
        Ok(Err(e)) => {
            eprintln!("{what}: {}: {e}", e.class_name());
            None
        }
        Err(p) => {
            eprintln!("{what}: panic: {}", panic_text(&*p));
            None
        }
    }
}

/// `Agent.selftest_loop` (misc): daily, the first run SELFTEST_FIRST_S after start.
///
/// # Errors
/// None: each round catches its own failures (the `LoopFn` shape).
#[allow(clippy::unnecessary_wraps)]
pub fn selftest_loop(d: &Daemon) -> AResult<()> {
    d.misc.selftest.run_loop(&d.stop, &d.inventory_due);
    Ok(())
}

/// A loop's entry point (runs until `daemon.stop`): a module's `fn(&Daemon) -> AResult<()>`, or a closure that
/// adapts a loop with its own signature.
pub type LoopFn = Box<dyn Fn(&Daemon) -> AResult<()> + Send>;

/// Runs `f` on its own thread, restarted (after 5 s) if it panics or fails; a stub (`NotPorted`) ends it.
fn spawn_loop(name: &'static str, daemon: &Arc<Daemon>, f: LoopFn) -> std::io::Result<()> {
    let d = Arc::clone(daemon);
    std::thread::Builder::new()
        .name(name.into())
        .spawn(move || {
            loop {
                match catch_unwind(AssertUnwindSafe(|| f(&d))) {
                    Ok(Ok(())) => return,
                    Ok(Err(AgentError::NotPorted(what))) => {
                        eprintln!("{name}: {what}: not yet ported to Rust");
                        return;
                    }
                    Ok(Err(e)) => eprintln!("{name} loop failed: {}: {e}", e.class_name()),
                    Err(p) => eprintln!("{name} loop failed: panic: {}", panic_text(&*p)),
                }
                if d.stop.wait(5.0) {
                    return;
                }
            }
        })
        .map(|_| ())
}

/// `Agent.run()`: the loops, then wait for `stop`. Telemetry (`EXPERIENCE=1`) is the Python sidecar
/// (`mc-telemetry.service`) in rust mode, not a thread here.
pub fn run(daemon: &Arc<Daemon>) -> std::io::Result<()> {
    let loops: Vec<(&'static str, LoopFn)> = vec![
        ("metrics", Box::new(crate::data::metrics_loop)),
        ("logs", Box::new(crate::ship::logs_loop)),
        ("control", Box::new(crate::link::control::control_loop)),
        ("inventory", Box::new(crate::data::inventory_loop)),
        ("panel", Box::new(crate::sync::panel_loop)),
        ("selftest", Box::new(selftest_loop)),
    ];
    let mut loops = loops;
    if daemon.settings.client {
        loops.push(("client", Box::new(crate::data::client_codes::client_loop)));
    }
    // `Executor(..., self.tail.request, ...)`: the console tail reads `Agent.ws` (data)
    crate::data::wire(daemon);
    // `Agent.panel_state()`'s parts from sync (`self.github is not None`) and misc (product, self-test)
    {
        let d = Arc::downgrade(daemon);
        let configured = crate::sync::github::github_from_env(
            &process_env(),
            Some(Arc::new(crate::sync::github::FixedToken(String::new()))),
        )
        .is_ok_and(|g| g.is_some());
        let _ = crate::data::PANEL_EXTRAS.set(Arc::new(move || {
            let Some(d) = d.upgrade() else {
                return Ok((configured, Value::Null, Value::Null));
            };
            let product = d.misc.product_state().map_or(Value::Null, Value::Object);
            let selftest = d.misc.selftest.state.public()?.unwrap_or(Value::Null);
            Ok((configured, product, selftest))
        }));
    }
    for (name, f) in loops {
        spawn_loop(name, daemon, f)?;
    }
    while !daemon.stop.wait(3600.0) {}
    Ok(())
}

/// Whether the stub gate is open (debug builds with `SMALLWORLD_AGENT_ALLOW_STUBS=1` only).
fn stubs_allowed() -> bool {
    cfg!(debug_assertions) && std::env::var(ALLOW_STUBS_ENV).is_ok_and(|v| v == "1")
}

/// `main()`: settings from the environment (exit 2 on a bad one), the state dir, the agent, the signals, the
/// startup line, the loops.
pub fn main() -> u8 {
    let settings = match Settings::from_env(&process_env()) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("mc-agent: {e}");
            return 2;
        }
    };
    if !NOT_PORTED.is_empty() && !stubs_allowed() {
        eprintln!(
            "smallworld-agent: the daemon is not yet ported to Rust (modules: {}); run mc_agent.py",
            NOT_PORTED.join(", ")
        );
        return 2;
    }
    // settings.state_dir.mkdir(parents=True, exist_ok=True): an error is an uncaught exception
    if let Err(e) = std::fs::create_dir_all(&settings.state_dir) {
        eprintln!("Traceback (most recent call last):");
        eprintln!("  File \"mc_agent.py\", in main");
        eprintln!(
            "{}: [Errno {}] {}: {}",
            smallworld_core::errno::io_class(&e),
            e.raw_os_error().unwrap_or(0),
            smallworld_core::errno::strerror(e.raw_os_error().unwrap_or(0)),
            smallworld_core::pyjson::repr_str(&settings.state_dir.to_string_lossy())
        );
        return 1;
    }
    let host = crate::settings::url_hostname(&settings.ingest_url).unwrap_or_else(|| "None".into());
    let daemon = match Daemon::new(settings) {
        Ok(d) => Arc::new(d),
        Err(e) => {
            // Agent(s) raising ValueError: an uncaught exception
            eprintln!("Traceback (most recent call last):");
            eprintln!("  File \"mc_agent.py\", in main");
            eprintln!("ValueError: {e}");
            return 1;
        }
    };
    // Before any thread starts: the signal mask is inherited.
    let stop = Arc::clone(&daemon.stop);
    crate::sys::handle_signals(&crate::sys::DAEMON_SIGNALS, move |_| stop.set());
    println!("mc-agent {VERSION}: pushing to {host}");
    match run(&daemon) {
        Ok(()) => 0,
        Err(e) => {
            eprintln!("mc-agent: can't start a thread: {e}");
            1
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rounds_log_and_survive() {
        assert_eq!(round("x round failed", || Ok(5)), Some(5));
        assert_eq!(
            round::<()>("x round failed", || Err(AgentError::Os(
                "TimeoutError".into()
            ))),
            None
        );
        assert_eq!(round::<()>("x round failed", || panic!("boom")), None);
    }

    #[test]
    fn stubs_stay_closed_without_the_switch() {
        if std::env::var(ALLOW_STUBS_ENV).is_err() {
            assert!(!stubs_allowed());
        }
        assert!(!NOT_PORTED.contains(&"link"));
    }
}
