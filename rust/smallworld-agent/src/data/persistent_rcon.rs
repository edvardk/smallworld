//! mc_rcon.py `PersistentRcon` (202-275): one long-lived RCON connection (the server logs a thread start/stop
//! for every connection), shared by the metrics loop and `status` through `stats_rcon` (mc_agent.py 427-441;
//! one per server dir). Any error closes it; the next call reconnects at once if a kept connection was dead, with
//! an exponential backoff (1 s doubling to `max_backoff`) after a failed connect. The factory reads
//! server.properties on every reconnect (a changed password or port is picked up).
//!
//! Python splits `connect()` (the settings: errors are `RconSettingsError`, nothing asked of the server) from
//! `__enter__` (socket + login: errors count as failures and back off); `Factory` returns a `Connector` for
//! exactly that split.

use smallworld_core::error::PyError;
use smallworld_core::mcrcon::{int_error, path_join, socket_port};
use smallworld_core::properties;
use smallworld_core::pystr::{PyInt, PyStr};
use smallworld_core::rcon::Rcon;
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, LazyLock, Mutex, PoisonError};
use std::time::Duration;

/// One command over an open connection (`Rcon.command(cmd, timeout)`).
pub trait RconConn: Send {
    /// # Errors
    /// The connection's `PyError`.
    fn command(&mut self, cmd: &str, timeout: Option<f64>) -> Result<String, PyError>;
}

impl RconConn for Rcon {
    fn command(&mut self, cmd: &str, timeout: Option<f64>) -> Result<String, PyError> {
        Rcon::command(self, &PyStr::from(cmd), timeout, false)
    }
}

/// `client.__enter__()`: connect and log in.
pub type Connector = Box<dyn FnOnce() -> Result<Box<dyn RconConn>, PyError> + Send>;
/// `factory()`: read the settings, return the (unconnected) client.
pub type Factory = Box<dyn Fn() -> Result<Connector, PyError> + Send + Sync>;
/// The clock (`time.monotonic`).
pub type Clock = Box<dyn Fn() -> f64 + Send + Sync>;

/// What `PersistentRcon.commands` raises, by the class `rcon_stats` catches.
#[derive(Debug)]
pub enum RconFailure {
    /// `RconSettingsError`: server.properties unreadable or RCON off.
    Settings(String),
    /// `RconAuthError` (the server answered: alive).
    Auth,
    /// `RconReplyError` (logged in, then the reply failed: alive).
    Reply(String),
    /// `OSError` / `RconError`: couldn't connect (or backing off).
    Down(PyError),
    /// Anything else (Python: not caught by `rcon_stats`).
    Other(PyError),
}

impl std::fmt::Display for RconFailure {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            RconFailure::Settings(m) | RconFailure::Reply(m) => f.write_str(m),
            RconFailure::Auth => f.write_str("RCON authentication failed"),
            RconFailure::Down(e) | RconFailure::Other(e) => write!(f, "{e}"),
        }
    }
}

struct State {
    conn: Option<Box<dyn RconConn>>,
    failures: u32,
    retry_at: f64,
    connects: u64,
}

/// mc_rcon.py `PersistentRcon`. Thread-safe: one call at a time.
pub struct PersistentRcon {
    factory: Factory,
    max_backoff: f64,
    clock: Clock,
    state: Mutex<State>,
}

/// Errors `factory()` raises that Python turns into `RconSettingsError` (`OSError, KeyError, ValueError,
/// RconError`; `UnicodeDecodeError` is a `ValueError`).
fn settings_class(e: &PyError) -> bool {
    e.is_caught()
        || matches!(
            e,
            PyError::Key(_)
                | PyError::Value(_)
                | PyError::UnicodeDecode(_)
                | PyError::UnicodeEncode(_)
        )
}

impl PersistentRcon {
    pub fn new(factory: Factory, max_backoff: f64, clock: Clock) -> Self {
        Self {
            factory,
            max_backoff,
            clock,
            state: Mutex::new(State {
                conn: None,
                failures: 0,
                retry_at: 0.0,
                connects: 0,
            }),
        }
    }

    /// Connections opened (tests; the e2e count of RCON client threads).
    pub fn connects(&self) -> u64 {
        self.state
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .connects
    }

    fn open(&self, st: &mut State) -> Result<Box<dyn RconConn>, RconFailure> {
        let now = (self.clock)();
        if now < st.retry_at {
            return Err(RconFailure::Down(PyError::Rcon(format!(
                "RCON reconnect backing off for {}s",
                crate::data::metrics::format_f0(st.retry_at - now)
            ))));
        }
        let connector = match (self.factory)() {
            Ok(c) => c,
            Err(e) if settings_class(&e) => {
                return Err(RconFailure::Settings(format!(
                    "RCON settings unreadable ({}: {e})",
                    e.class_name()
                )));
            }
            Err(e) => return Err(RconFailure::Other(e)),
        };
        st.connects += 1;
        match connector() {
            Ok(c) => {
                st.failures = 0;
                st.retry_at = 0.0;
                Ok(c)
            }
            Err(e) => {
                st.failures += 1;
                let exp = i32::try_from(st.failures - 1).unwrap_or(i32::MAX);
                st.retry_at = now + self.max_backoff.min(2f64.powi(exp));
                Err(match e {
                    PyError::RconAuth => RconFailure::Auth,
                    e if e.is_caught() => RconFailure::Down(e),
                    e => RconFailure::Other(e),
                })
            }
        }
    }

    /// `commands(cmds, timeout)`: each command's reply, in order.
    ///
    /// # Errors
    /// See `RconFailure`.
    pub fn commands(
        &self,
        cmds: &[&str],
        timeout: Option<f64>,
    ) -> Result<Vec<String>, RconFailure> {
        let mut st = self.state.lock().unwrap_or_else(PoisonError::into_inner);
        let mut kept = st.conn.is_some();
        loop {
            if st.conn.is_none() {
                let c = self.open(&mut st)?;
                st.conn = Some(c);
            }
            let conn = st.conn.as_mut().expect("just opened");
            let mut out = Vec::with_capacity(cmds.len());
            let mut failed = None;
            for c in cmds {
                match conn.command(c, timeout) {
                    Ok(r) => out.push(r),
                    Err(e) => {
                        failed = Some(e);
                        break;
                    }
                }
            }
            match failed {
                None => return Ok(out),
                Some(e) if e.is_caught() => {
                    st.conn = None;
                    if !kept {
                        return Err(RconFailure::Reply(format!("{}: {e}", e.class_name())));
                    }
                    kept = false;
                }
                // not `(OSError, RconError)`: propagates, the connection stays (Python's except doesn't run)
                Some(e) => return Err(RconFailure::Other(e)),
            }
        }
    }

    /// `command(cmd, timeout)`.
    ///
    /// # Errors
    /// See `RconFailure`.
    pub fn command(&self, cmd: &str, timeout: Option<f64>) -> Result<String, RconFailure> {
        Ok(self.commands(&[cmd], timeout)?.remove(0))
    }

    /// `close()`.
    pub fn close(&self) {
        self.state
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .conn = None;
    }
}

/// `mc_rcon.connect(server_dir, timeout)` split like Python's `connect()` + `__enter__`.
///
/// # Errors
/// The settings errors (`connect()`'s).
pub fn connect_factory(server_dir: &Path, timeout: f64) -> Result<Connector, PyError> {
    use std::os::unix::ffi::OsStrExt;
    let dir = PyStr::from_os_bytes(server_dir.as_os_str().as_bytes());
    let props = properties::read_properties(&path_join(&dir, "server.properties"))?;
    let get = |key: &str| props.get(&PyStr::from(key));
    if !get("enable-rcon").is_some_and(|v| v.eq_str("true")) {
        return Err(PyError::Rcon(
            "RCON is disabled in server.properties".into(),
        ));
    }
    let host = match get("rcon.ip") {
        Some(ip) if !ip.is_empty() => ip.clone(),
        _ => PyStr::from("127.0.0.1"),
    };
    let port_text = get("rcon.port")
        .cloned()
        .unwrap_or_else(|| PyStr::from("25575"));
    let port = PyInt::parse(&port_text).map_err(|e| int_error(&port_text, &e))?;
    let password = get("rcon.password")
        .cloned()
        .ok_or_else(|| PyError::Key(PyStr::from("rcon.password")))?;
    Ok(Box::new(move || {
        let host = host.to_string_checked().ok_or_else(|| {
            PyError::UnicodeEncode(format!(
                "encoding with 'idna' codec failed (UnicodeEncodeError: 'idna' codec can't encode {})",
                host.repr()
            ))
        })?;
        let rcon = Rcon::connect(
            &host,
            socket_port(&port)?,
            &password,
            Duration::from_secs_f64(timeout),
        )?;
        Ok(Box::new(rcon) as Box<dyn RconConn>)
    }))
}

/// mc_agent.py `RCON_TIMEOUT_S`.
pub const RCON_TIMEOUT_S: f64 = 5.0;

static RCON_CLIENTS: LazyLock<Mutex<HashMap<PathBuf, Arc<PersistentRcon>>>> =
    LazyLock::new(|| Mutex::new(HashMap::new()));

/// mc_agent.py `stats_rcon(server_dir)`: the kept connection for a server dir (created on first use).
pub fn stats_rcon(server_dir: &Path) -> Arc<PersistentRcon> {
    let mut clients = RCON_CLIENTS.lock().unwrap_or_else(PoisonError::into_inner);
    clients
        .entry(server_dir.to_path_buf())
        .or_insert_with(|| {
            let dir = server_dir.to_path_buf();
            Arc::new(PersistentRcon::new(
                Box::new(move || connect_factory(&dir, RCON_TIMEOUT_S)),
                30.0,
                Box::new(crate::sys::monotonic),
            ))
        })
        .clone()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU32, Ordering};

    struct Fake {
        replies: Arc<Mutex<Vec<Result<String, PyError>>>>,
    }
    impl RconConn for Fake {
        fn command(&mut self, _cmd: &str, _t: Option<f64>) -> Result<String, PyError> {
            self.replies.lock().unwrap().remove(0)
        }
    }

    type Script = Arc<Mutex<Vec<Result<Vec<Result<String, PyError>>, PyError>>>>;

    fn rcon(script: Script, clock: Arc<Mutex<f64>>) -> PersistentRcon {
        PersistentRcon::new(
            Box::new(move || {
                let script = script.clone();
                Ok(Box::new(move || {
                    let replies = script.lock().unwrap().remove(0)?;
                    Ok(Box::new(Fake {
                        replies: Arc::new(Mutex::new(replies)),
                    }) as Box<dyn RconConn>)
                }) as Connector)
            }),
            30.0,
            Box::new(move || *clock.lock().unwrap()),
        )
    }

    fn refused() -> PyError {
        PyError::Os {
            errno: 111,
            strerror: "Connection refused".into(),
            filename: None,
        }
    }

    #[test]
    fn keeps_one_connection_and_reconnects_once_after_a_dead_one() {
        let clock = Arc::new(Mutex::new(100.0));
        let script: Script = Arc::new(Mutex::new(vec![
            Ok(vec![
                Ok("a".into()),
                Ok("b".into()),
                Err(PyError::Rcon("connection closed by server".into())),
            ]),
            Ok(vec![Ok("c".into())]),
        ]));
        let r = rcon(script, clock);
        assert_eq!(r.commands(&["x", "y"], None).unwrap(), vec!["a", "b"]);
        assert_eq!(r.connects(), 1);
        // the kept connection is dead: one fresh try, transparently
        assert_eq!(r.command("z", None).unwrap(), "c");
        assert_eq!(r.connects(), 2);
    }

    #[test]
    fn a_fresh_connection_failing_is_a_reply_error_and_auth_is_alive() {
        let clock = Arc::new(Mutex::new(0.0));
        let script: Script = Arc::new(Mutex::new(vec![
            Ok(vec![Err(PyError::Timeout)]),
            Err(PyError::RconAuth),
        ]));
        let r = rcon(script, clock.clone());
        match r.command("x", None) {
            Err(RconFailure::Reply(m)) => assert_eq!(m, "TimeoutError: timed out"),
            other => panic!("{other:?}"),
        }
        assert!(matches!(r.command("x", None), Err(RconFailure::Auth)));
        // failed connect: back off 1 s (doubling)
        match r.command("x", None) {
            Err(RconFailure::Down(e)) => {
                assert_eq!(e.to_string(), "RCON reconnect backing off for 1s");
            }
            other => panic!("{other:?}"),
        }
    }

    #[test]
    fn backoff_doubles_to_the_cap() {
        let clock = Arc::new(Mutex::new(0.0));
        let n = Arc::new(AtomicU32::new(0));
        let n2 = n.clone();
        let c2 = clock.clone();
        let r = PersistentRcon::new(
            Box::new(move || {
                n2.fetch_add(1, Ordering::SeqCst);
                Ok(Box::new(|| Err(refused())) as Connector)
            }),
            30.0,
            Box::new(move || *c2.lock().unwrap()),
        );
        let mut waits = Vec::new();
        for _ in 0..7 {
            assert!(matches!(r.command("x", None), Err(RconFailure::Down(_))));
            let at = *clock.lock().unwrap();
            // find when the next try is allowed
            let mut t = at;
            loop {
                *clock.lock().unwrap() = t;
                match r.command("x", None) {
                    Err(RconFailure::Down(PyError::Rcon(_))) => t += 0.5,
                    _ => break,
                }
            }
            waits.push(t - at);
        }
        assert_eq!(waits, vec![1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]);
    }

    #[test]
    fn settings_errors() {
        let r = PersistentRcon::new(
            Box::new(|| Err(PyError::Key(PyStr::from("rcon.password")))),
            30.0,
            Box::new(|| 0.0),
        );
        match r.command("x", None) {
            Err(RconFailure::Settings(m)) => {
                assert_eq!(m, "RCON settings unreadable (KeyError: 'rcon.password')");
            }
            other => panic!("{other:?}"),
        }
        assert_eq!(r.connects(), 0);
    }
}
