//! mc_panel.py's job runner (`Job`, `say_text`, `Jobs`, `rcon_command`, `wait_for_rcon`; panel:1042-1250).
//!
//! At most one job at a time per process (`Jobs.lock`) and across processes (`flock(job.lock, LOCK_EX|LOCK_NB)`,
//! interlocking with the Python daemon and cli: smallworld_core::flock). A job counts down in-game (`tellraw` +
//! action bar at `ANNOUNCE_AT`), skipped when nobody is online, cancellable only during the countdown; then its
//! work runs. Every state change goes to `report` (daemon: `POST /ingest/job`; cli: progress lines).
//!
//! Owner: jobs. Ported: the whole runner, foreground (`background: false`, the cli) and background (a thread per
//! job, the daemon); `cancel`. The daemon's job reports are link's `daemon::report_job` (mc_agent.py
//! `Agent.report_job`: `POST /ingest/job`, `WARN` "job", then `inventory_due`), the runner it builds
//! `Jobs::new(report, Some(JOB_LOCK_FILE), true)`; the panel's cancel is actions' `do_cancel` ->
//! `Jobs::cancel` (an `Action` error becomes `Rejected("cancel: ...")` in `Executor::run`).
//!
//! A SIGTERM during a countdown: the cli cancels the job (cli.rs); the daemon just stops (mc_agent.py `main`
//! sets `stop`, the job threads are daemon threads): nothing more is reported, and the kernel releases
//! job.lock when the process exits.

use crate::types::{ANNOUNCE_AT, AResult, AgentError, COUNTDOWNS, Value};
use regex::Regex;
use serde_json::json;
use smallworld_core::flock::{JOB_BUSY, JobLockGuard, take_job_lock};
use smallworld_core::pyjson::string as js;
use smallworld_core::pystr::{PyStr, prefix_cp};
use std::path::PathBuf;
use std::sync::{Arc, Condvar, LazyLock, Mutex, MutexGuard, PoisonError};
use std::time::{Duration, Instant};

fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(PoisonError::into_inner)
}

/// `threading.Event`.
#[derive(Debug, Default)]
pub struct Event {
    flag: Mutex<bool>,
    cv: Condvar,
}

impl Event {
    pub fn set(&self) {
        *lock(&self.flag) = true;
        self.cv.notify_all();
    }

    pub fn is_set(&self) -> bool {
        *lock(&self.flag)
    }

    /// `event.clear()`.
    pub fn clear(&self) {
        *lock(&self.flag) = false;
    }

    /// `event.wait(seconds)`: true if set (at once or within the time).
    pub fn wait(&self, seconds: f64) -> bool {
        let deadline = Instant::now() + Duration::from_secs_f64(seconds.max(0.0));
        let mut set = lock(&self.flag);
        while !*set {
            let now = Instant::now();
            if now >= deadline {
                return false;
            }
            set = self
                .cv
                .wait_timeout(set, deadline - now)
                .unwrap_or_else(PoisonError::into_inner)
                .0;
        }
        true
    }
}

/// A job's mutable state (Python's dataclass fields that change).
#[derive(Debug, Clone, PartialEq)]
pub struct JobState {
    /// countdown | running | done | failed | cancelled
    pub state: String,
    pub step: String,
    pub deadline: f64,
    pub started: f64,
    pub finished: f64,
    pub players: i64,
}

pub type Report = Arc<dyn Fn(&Value) + Send + Sync>;

/// mc_panel.py `Job`.
pub struct Job {
    pub id: String,
    pub action: String,
    pub label: String,
    pub login: String,
    pub countdown: i64,
    pub cancel: Event,
    inner: Mutex<JobState>,
    report: Report,
}

impl std::fmt::Debug for Job {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Job")
            .field("id", &self.id)
            .field("action", &self.action)
            .field("state", &self.snapshot())
            .finish_non_exhaustive()
    }
}

impl Job {
    pub fn snapshot(&self) -> JobState {
        lock(&self.inner).clone()
    }

    pub fn state(&self) -> String {
        lock(&self.inner).state.clone()
    }

    pub fn step(&self) -> String {
        lock(&self.inner).step.clone()
    }

    /// `set_step(step)`: changes the step and reports the job.
    pub fn set_step(&self, step: &str) {
        lock(&self.inner).step = step.to_string();
        (self.report)(&self.public());
    }

    fn with<R>(&self, f: impl FnOnce(&mut JobState) -> R) -> R {
        f(&mut lock(&self.inner))
    }

    /// `public()`: what the panel sees (times in ms, `step[:300]`).
    pub fn public(&self) -> Value {
        let s = self.snapshot();
        #[allow(clippy::cast_possible_truncation)]
        let ms = |t: f64| (t * 1000.0).trunc() as i64;
        json!({
            "id": self.id, "action": self.action, "label": self.label, "login": self.login,
            "state": s.state, "step": prefix_cp(&s.step, 300), "countdown": self.countdown,
            "deadline": ms(s.deadline), "started": ms(s.started),
            "finished": if s.finished == 0.0 { Value::Null } else { json!(ms(s.finished)) },
            "players": s.players,
        })
    }
}

/// `say_text(label, left)`: "Restart in 5 minutes. Find a safe spot!".
pub fn say_text(label: &str, left: i64) -> String {
    let when = if left >= 60 {
        format!("{} minute{}", left / 60, if left >= 120 { "s" } else { "" })
    } else {
        format!("{left} second{}", if left == 1 { "" } else { "s" })
    };
    format!("{label} in {when}. Find a safe spot!")
}

/// `rcon_command(line)`: a fresh connection (10 s) for one command.
///
/// # Errors
/// The RCON/properties error, sorted (types.rs).
pub fn rcon_command(line: &str) -> AResult<String> {
    let mut r = smallworld_core::mcrcon::connect_with(None, Duration::from_secs(10))?;
    Ok(r.command(&PyStr::from(line), None, false)?)
}

/// `wait_for_rcon(timeout)`: true once `list` answers, polling every 5 s.
///
/// # Errors
/// Only what Python doesn't swallow (`Crash`).
pub fn wait_for_rcon(
    timeout: f64,
    rcon: &dyn Fn(&str) -> AResult<String>,
    sleep: &dyn Fn(f64),
) -> AResult<bool> {
    let deadline = Instant::now() + Duration::from_secs_f64(timeout);
    while Instant::now() < deadline {
        match rcon("list") {
            Ok(_) => return Ok(true),
            Err(e) if e.is_rcon_soft() => {}
            Err(e) => return Err(e),
        }
        sleep(5.0);
    }
    Ok(false)
}

static PLAYERS_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"There are (\d+) of a max").unwrap());

pub type RconFn = Arc<dyn Fn(&str) -> AResult<String> + Send + Sync>;
pub type WaitFn = Arc<dyn Fn(&Event, f64) -> bool + Send + Sync>;
pub type ClockFn = Arc<dyn Fn() -> f64 + Send + Sync>;
/// A job's work: returns its final step text.
pub type Work = Box<dyn FnOnce(&Arc<Job>) -> AResult<String> + Send>;

/// mc_panel.py `Jobs`.
pub struct Jobs {
    report: Mutex<Report>,
    rcon: RconFn,
    clock: ClockFn,
    wait: WaitFn,
    current: Mutex<Option<Arc<Job>>>,
    lock: Mutex<()>,
    lock_path: Option<PathBuf>,
    background: bool,
    held: Mutex<Option<JobLockGuard>>,
}

impl Jobs {
    /// `Jobs(report, rcon=None, clock=time.time, wait=None, lock_path=None, background=True)`.
    pub fn new(report: Report, lock_path: Option<PathBuf>, background: bool) -> Arc<Self> {
        Self::with_parts(
            report,
            Arc::new(rcon_command),
            Arc::new(crate::sys::now),
            Arc::new(|ev: &Event, s: f64| ev.wait(s)),
            lock_path,
            background,
        )
    }

    pub fn with_parts(
        report: Report,
        rcon: RconFn,
        clock: ClockFn,
        wait: WaitFn,
        lock_path: Option<PathBuf>,
        background: bool,
    ) -> Arc<Self> {
        Arc::new(Self {
            report: Mutex::new(report),
            rcon,
            clock,
            wait,
            current: Mutex::new(None),
            lock: Mutex::new(()),
            lock_path,
            background,
            held: Mutex::new(None),
        })
    }

    /// `jobs.report = report` (the cli swaps in its printer).
    pub fn set_report(&self, report: Report) {
        *lock(&self.report) = report;
    }

    fn report_fn(&self) -> Report {
        lock(&self.report).clone()
    }

    pub fn current(&self) -> Option<Arc<Job>> {
        lock(&self.current).clone()
    }

    /// `busy()`: the job in countdown or running, if any.
    pub fn busy(&self) -> Option<Arc<Job>> {
        self.current()
            .filter(|j| matches!(j.state().as_str(), "countdown" | "running"))
    }

    /// `players()`: from `list`, or 0 when the server doesn't answer.
    ///
    /// # Errors
    /// Only `Crash` (what Python doesn't catch).
    pub fn players(&self) -> AResult<i64> {
        let reply = match (self.rcon)("list") {
            Ok(r) => r,
            Err(e) if e.is_rcon_soft() => return Ok(0),
            Err(e) => return Err(e),
        };
        let text = smallworld_core::rcon::strip_formatting(&reply);
        Ok(PLAYERS_RE
            .captures(&text)
            .and_then(|c| crate::unicode::parse_decimal(&c[1]))
            .unwrap_or(0))
    }

    /// `announce(text)`: `tellraw @a` and `title @a actionbar`, the exact JSON components Python sends.
    ///
    /// # Errors
    /// Only `Crash`.
    pub fn announce(&self, text: &str) -> AResult<()> {
        let t = js(text, true);
        let component =
            format!(r#"["", {{"text": "[Server] ", "color": "gold"}}, {{"text": {t}}}]"#);
        let bar = format!(r#"{{"text": {t}, "color": "gold"}}"#);
        for line in [
            format!("tellraw @a {component}"),
            format!("title @a actionbar {bar}"),
        ] {
            match (self.rcon)(&line) {
                Ok(_) => {}
                Err(e) if e.is_rcon_soft() => return Ok(()),
                Err(e) => return Err(e),
            }
        }
        Ok(())
    }

    /// `start(job_id, action, label, login, countdown, work, skip_if_empty)`. In the foreground the job has run
    /// when this returns.
    ///
    /// # Errors
    /// `Action` (bad countdown, busy here or in the other process); in the foreground, a `Crash` of the job.
    #[allow(clippy::too_many_arguments)]
    pub fn start(
        self: &Arc<Self>,
        job_id: &str,
        action: &str,
        label: &str,
        login: &str,
        countdown: i64,
        work: Work,
        skip_if_empty: bool,
    ) -> AResult<Arc<Job>> {
        if !COUNTDOWNS.contains(&countdown) {
            return Err(AgentError::Action(format!(
                "countdown must be one of {} seconds",
                COUNTDOWNS.map(|c| c.to_string()).join(", ")
            )));
        }
        let job = {
            let _g = lock(&self.lock);
            if let Some(other) = self.busy() {
                return Err(AgentError::Action(format!(
                    "busy: {} ({}) started by {}; cancel it first",
                    other.label.to_lowercase(),
                    other.state(),
                    other.login
                )));
            }
            if let Some(path) = &self.lock_path {
                match take_job_lock(path) {
                    Ok(guard) => *lock(&self.held) = Some(guard),
                    Err(_) => return Err(AgentError::Action(JOB_BUSY.into())),
                }
            }
            let report = self.report_fn();
            let job = Arc::new(Job {
                id: job_id.into(),
                action: action.into(),
                label: label.into(),
                login: login.into(),
                countdown,
                cancel: Event::default(),
                inner: Mutex::new(JobState {
                    state: "countdown".into(),
                    step: String::new(),
                    deadline: 0.0,
                    started: (self.clock)(),
                    finished: 0.0,
                    players: 0,
                }),
                // on_change reports through whatever report is current then (the cli swaps it in before).
                report: Arc::new({
                    let jobs = Arc::downgrade(self);
                    move |v: &Value| {
                        if let Some(j) = jobs.upgrade() {
                            (j.report_fn())(v);
                        } else {
                            report(v);
                        }
                    }
                }),
            });
            *lock(&self.current) = Some(job.clone());
            job
        };
        if self.background {
            let (me, j) = (self.clone(), job.clone());
            std::thread::Builder::new()
                .name(format!("job-{action}"))
                .spawn(move || {
                    if let Err(e) = me.run(&j, work, skip_if_empty) {
                        // Python: the thread dies with a traceback; the job stays as it was.
                        eprintln!("Exception in thread job-{}: {e}", j.action);
                    }
                })
                .map_err(AgentError::from)?;
        } else {
            self.run(&job, work, skip_if_empty)?;
        }
        Ok(job)
    }

    /// `_run`: the work, then the file lock is released whatever happened.
    fn run(&self, job: &Arc<Job>, work: Work, skip_if_empty: bool) -> AResult<()> {
        let r = self.work(job, work, skip_if_empty);
        lock(&self.held).take();
        r
    }

    fn report_job(&self, job: &Job) {
        (self.report_fn())(&job.public());
    }

    /// `_work`: the countdown, then the work; the job ends done, failed or cancelled.
    fn work(&self, job: &Arc<Job>, work: Work, skip_if_empty: bool) -> AResult<()> {
        let outcome = self.countdown_then(job, work, skip_if_empty);
        match outcome {
            Ok(true) => return Ok(()), // cancelled: already reported
            Ok(false) => {}
            Err(AgentError::Action(m)) => job.with(|s| {
                s.state = "failed".into();
                s.step = m;
            }),
            Err(e @ (AgentError::Os(_) | AgentError::Value(..))) => job.with(|s| {
                let what = if s.step.is_empty() {
                    job.action.clone()
                } else {
                    s.step.clone()
                };
                s.state = "failed".into();
                s.step = format!("{what} failed: {}", e.class_name());
            }),
            Err(AgentError::NotPorted(what)) => job.with(|s| {
                s.state = "failed".into();
                s.step = format!("{what}: not yet ported to Rust");
            }),
            Err(other) => return Err(other),
        }
        job.with(|s| s.finished = (self.clock)());
        self.report_job(job);
        Ok(())
    }

    /// The countdown and the work. Ok(true) = cancelled (reported), Ok(false) = done (state set, not reported).
    fn countdown_then(&self, job: &Arc<Job>, work: Work, skip_if_empty: bool) -> AResult<bool> {
        let players = if job.countdown != 0 {
            self.players()?
        } else {
            0
        };
        let mut left = if players != 0 || !skip_if_empty {
            job.countdown
        } else {
            0
        };
        let now = (self.clock)();
        job.with(|s| {
            s.players = players;
            #[allow(clippy::cast_precision_loss)]
            {
                s.deadline = now + left as f64;
            }
            if left != 0 {
                s.step = format!("countdown {left} s, {players} player(s) online");
            }
        });
        self.report_job(job);
        while left > 0 {
            if ANNOUNCE_AT.contains(&left) || left == job.countdown {
                self.announce(&say_text(&job.label, left))?;
            }
            let next = ANNOUNCE_AT
                .iter()
                .copied()
                .filter(|&t| t < left)
                .max()
                .unwrap_or(0);
            #[allow(clippy::cast_precision_loss)]
            let cancelled = (self.wait)(&job.cancel, (left - next) as f64);
            if cancelled {
                self.announce(&format!("{} cancelled.", job.label))?;
                let now = (self.clock)();
                job.with(|s| {
                    s.state = "cancelled".into();
                    s.step = "cancelled during the countdown".into();
                    s.finished = now;
                });
                self.report_job(job);
                return Ok(true);
            }
            left = next;
            if left >= 10 && skip_if_empty && players != 0 && self.players()? == 0 {
                job.with(|s| s.step = "everyone left: no need to wait".into());
                left = 0;
            }
        }
        let now = (self.clock)();
        job.with(|s| {
            s.state = "running".into();
            s.deadline = now;
        });
        self.report_job(job);
        let step = work(job)?;
        job.with(|s| {
            s.step = if step.is_empty() { "done".into() } else { step };
            s.state = "done".into();
        });
        Ok(false)
    }

    /// `cancel(login)`: cancels the job in countdown.
    ///
    /// # Errors
    /// `Action` when there is nothing to cancel or the job is already running.
    pub fn cancel(&self, login: &str) -> AResult<String> {
        let Some(job) = self.busy() else {
            return Err(AgentError::Action("nothing to cancel".into()));
        };
        if job.state() != "countdown" {
            return Err(AgentError::Action(format!(
                "{} is already running; it can't be cancelled now",
                job.label.to_lowercase()
            )));
        }
        job.cancel.set();
        Ok(format!("{} cancelled by {login}", job.label.to_lowercase()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Fake {
        lines: Mutex<Vec<String>>,
        players: Mutex<Vec<i64>>,
    }

    fn jobs(
        fake: &Arc<Fake>,
        reports: &Arc<Mutex<Vec<Value>>>,
        cancel_at: Option<usize>,
    ) -> Arc<Jobs> {
        let (f1, r1) = (fake.clone(), reports.clone());
        let waits = Arc::new(Mutex::new(0usize));
        Jobs::with_parts(
            Arc::new(move |v: &Value| r1.lock().unwrap().push(v.clone())),
            Arc::new(move |line: &str| {
                f1.lines.lock().unwrap().push(line.to_string());
                if line == "list" {
                    let mut p = f1.players.lock().unwrap();
                    let n = if p.len() > 1 { p.remove(0) } else { p[0] };
                    return Ok(format!("There are {n} of a max of 20 players online:"));
                }
                Ok(String::new())
            }),
            Arc::new(|| 1000.0),
            Arc::new(move |_ev: &Event, _s: f64| {
                let mut w = waits.lock().unwrap();
                *w += 1;
                cancel_at == Some(*w)
            }),
            None,
            false,
        )
    }

    #[test]
    fn say_text_like_python() {
        assert_eq!(
            say_text("Restart", 300),
            "Restart in 5 minutes. Find a safe spot!"
        );
        assert_eq!(
            say_text("Restart", 60),
            "Restart in 1 minute. Find a safe spot!"
        );
        assert_eq!(
            say_text("Restart", 1),
            "Restart in 1 second. Find a safe spot!"
        );
        assert_eq!(
            say_text("Restart", 30),
            "Restart in 30 seconds. Find a safe spot!"
        );
    }

    #[test]
    fn countdown_announces_and_runs() {
        let fake = Arc::new(Fake {
            lines: Mutex::new(vec![]),
            players: Mutex::new(vec![2]),
        });
        let reports = Arc::new(Mutex::new(vec![]));
        let j = jobs(&fake, &reports, None);
        let job = j
            .start(
                "c1",
                "restart",
                "Restart",
                "mcctl:ed",
                10,
                Box::new(|job| {
                    job.set_step("working");
                    Ok("restarted".into())
                }),
                true,
            )
            .unwrap();
        assert_eq!(job.state(), "done");
        assert_eq!(job.step(), "restarted");
        let lines = fake.lines.lock().unwrap().clone();
        assert_eq!(lines[0], "list");
        assert_eq!(
            lines[1],
            r#"tellraw @a ["", {"text": "[Server] ", "color": "gold"}, {"text": "Restart in 10 seconds. Find a safe spot!"}]"#
        );
        assert_eq!(
            lines[2],
            r#"title @a actionbar {"text": "Restart in 10 seconds. Find a safe spot!", "color": "gold"}"#
        );
        let states: Vec<String> = reports
            .lock()
            .unwrap()
            .iter()
            .map(|r| {
                format!(
                    "{}: {}",
                    r["state"].as_str().unwrap(),
                    r["step"].as_str().unwrap()
                )
            })
            .collect();
        assert_eq!(
            states.first().unwrap(),
            "countdown: countdown 10 s, 2 player(s) online"
        );
        assert!(states.contains(&"running: countdown 10 s, 2 player(s) online".to_string()));
        assert_eq!(states.last().unwrap(), "done: restarted");
    }

    #[test]
    fn empty_server_skips_and_cancel_works() {
        let fake = Arc::new(Fake {
            lines: Mutex::new(vec![]),
            players: Mutex::new(vec![0]),
        });
        let reports = Arc::new(Mutex::new(vec![]));
        let j = jobs(&fake, &reports, None);
        let job = j
            .start(
                "c2",
                "plugin",
                "Restart",
                "x",
                60,
                Box::new(|_| Ok(String::new())),
                true,
            )
            .unwrap();
        assert_eq!((job.state(), job.step()), ("done".into(), "done".into()));
        assert_eq!(fake.lines.lock().unwrap().as_slice(), ["list"]);

        let fake = Arc::new(Fake {
            lines: Mutex::new(vec![]),
            players: Mutex::new(vec![1]),
        });
        let j = jobs(&fake, &reports, Some(1));
        let job = j
            .start(
                "c3",
                "restart",
                "Restart",
                "x",
                10,
                Box::new(|_| panic!("must not run")),
                true,
            )
            .unwrap();
        assert_eq!(job.state(), "cancelled");
        assert!(
            fake.lines
                .lock()
                .unwrap()
                .last()
                .unwrap()
                .contains("Restart cancelled.")
        );
    }

    #[test]
    fn failures_and_busy() {
        let fake = Arc::new(Fake {
            lines: Mutex::new(vec![]),
            players: Mutex::new(vec![0]),
        });
        let reports = Arc::new(Mutex::new(vec![]));
        let j = jobs(&fake, &reports, None);
        let job = j
            .start(
                "c4",
                "plugin",
                "Restart",
                "x",
                0,
                Box::new(|job| {
                    job.set_step("moving the jar");
                    Err(AgentError::Os("CalledProcessError".into()))
                }),
                true,
            )
            .unwrap();
        assert_eq!(job.step(), "moving the jar failed: CalledProcessError");
        assert_eq!(job.state(), "failed");
        let job = j
            .start(
                "c5",
                "x",
                "X",
                "x",
                0,
                Box::new(|_| {
                    Err(AgentError::Action(
                        "restarted, but RCON didn't answer within 5 minutes".into(),
                    ))
                }),
                true,
            )
            .unwrap();
        assert_eq!(
            job.step(),
            "restarted, but RCON didn't answer within 5 minutes"
        );
        assert!(
            j.start(
                "c6",
                "x",
                "X",
                "x",
                5,
                Box::new(|_| Ok(String::new())),
                true
            )
            .is_err()
        );
        assert_eq!(
            j.cancel("me").unwrap_err(),
            AgentError::Action("nothing to cancel".into())
        );
    }

    #[test]
    fn the_file_lock_is_shared_across_runners() {
        let path = std::env::temp_dir().join(format!("swagent-joblock-{}", std::process::id()));
        let guard = take_job_lock(&path).unwrap();
        let j = Jobs::new(Arc::new(|_v: &Value| {}), Some(path.clone()), false);
        let e = j
            .start(
                "c",
                "restart",
                "Restart",
                "x",
                0,
                Box::new(|_| Ok(String::new())),
                true,
            )
            .unwrap_err();
        assert_eq!(e, AgentError::Action(JOB_BUSY.into()));
        drop(guard);
        assert!(
            j.start(
                "c",
                "restart",
                "Restart",
                "x",
                0,
                Box::new(|_| Ok(String::new())),
                true
            )
            .is_ok()
        );
        // released after the job (retried: a concurrent test's fork may share the descriptor until its exec)
        let deadline = Instant::now() + Duration::from_secs(5);
        while take_job_lock(&path).is_err() {
            assert!(Instant::now() < deadline, "the job lock wasn't released");
            std::thread::sleep(Duration::from_millis(10));
        }
    }

    #[test]
    fn event_waits() {
        let e = Event::default();
        assert!(!e.wait(0.01));
        e.set();
        assert!(e.wait(10.0));
    }
}
