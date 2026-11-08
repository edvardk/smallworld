//! The operations behind the subcommands: mc_rcon.py's `connect`, `online_players`, `stop`, `countdown`,
//! `worldborder` and `gamerule`, in the same order of steps, with the same messages.

use crate::error::PyError;
use crate::properties;
use crate::pystr::{IntError, PyInt, PyStr};
use crate::rcon::Rcon;
use std::io::Write;
use std::os::unix::ffi::OsStrExt;
use std::path::PathBuf;
use std::process::Command as Process;
use std::time::{Duration, Instant};

/// `connect()`'s default timeout, and the socket timeout of every command that doesn't set its own.
const TIMEOUT: Duration = Duration::from_secs(10);

/// stop(): the whole stop (flush, stop, wait) stays within this, below the unit's `TimeoutStopSec=120`.
const STOP_BUDGET: f64 = 110.0;

/// Prints a line to stdout, like Python's `print()`. (A closed stdout is ignored rather than crashing.)
pub fn say(line: &str) {
    let _ = writeln!(std::io::stdout(), "{line}");
}

/// An environment variable as Python's `os.environ.get` sees it.
pub fn env(name: &str) -> Option<PyStr> {
    std::env::var_os(name).map(|v| PyStr::from_os_bytes(v.as_bytes()))
}

/// `Path(text) / name` as text: pathlib drops empty and `.` parts and trailing slashes, and keeps exactly two
/// leading slashes (POSIX lets them mean something) but folds three or more into one.
fn path_join(dir: &PyStr, name: &str) -> PyStr {
    let slash = u32::from('/');
    let cps = dir.code_points();
    let root = if cps.starts_with(&[slash, slash]) && cps.get(2) != Some(&slash) {
        "//"
    } else if cps.first() == Some(&slash) {
        "/"
    } else {
        ""
    };
    let mut out = PyStr::from(root);
    for part in cps.split(|&c| c == slash) {
        if part.is_empty() || part == [u32::from('.')] {
            continue;
        }
        out.push_py(&PyStr::from_code_points(part));
        out.push(slash);
    }
    out.push_str(name);
    out
}

/// `SERVER_DIR / "server.properties"`, `SERVER_DIR` coming from `$MC_SERVER_DIR` (default /opt/minecraft/server).
fn properties_path() -> PyStr {
    let dir = env("MC_SERVER_DIR").unwrap_or_else(|| PyStr::from("/opt/minecraft/server"));
    path_join(&dir, "server.properties")
}

/// `int(text)`'s `ValueError`, worded as Python words it.
fn int_error(text: &PyStr, e: &IntError) -> PyError {
    match e {
        IntError::Invalid => PyError::Value(format!(
            "invalid literal for int() with base 10: {}",
            text.repr()
        )),
        IntError::TooManyDigits(n) => PyError::Value(format!(
            "Exceeds the limit (4300 digits) for integer string conversion: value has {n} digits; use \
             sys.set_int_max_str_digits() to increase the limit"
        )),
    }
}

/// The port the system call gets for `rcon.port`, the way Python and glibc's `getaddrinfo` treat it: taken
/// modulo 65536, refused (`gaierror -8`) when negative or too big.
fn socket_port(port: &PyInt) -> Result<u16, PyError> {
    let refused = || PyError::Gai {
        code: -8,
        message: "Servname not supported for ai_socktype".into(),
    };
    match port.to_i128() {
        Some(p) if (0..i128::from(i64::MAX)).contains(&p) => {
            Ok(u16::try_from(p % 65536).unwrap_or(0))
        }
        _ => Err(refused()),
    }
}

/// `connect()`: read server.properties, then connect and log in.
pub fn connect() -> Result<Rcon, PyError> {
    let props = properties::read_properties(&properties_path())?;
    let get = |key: &str| props.get(&PyStr::from(key));
    if !get("enable-rcon").is_some_and(|v| v.eq_str("true")) {
        return Err(PyError::Rcon(
            "RCON is disabled in server.properties".into(),
        ));
    }
    // `props.get("rcon.ip") or "127.0.0.1"`: missing or empty means localhost.
    let host = match get("rcon.ip") {
        Some(ip) if !ip.is_empty() => ip.clone(),
        _ => PyStr::from("127.0.0.1"),
    };
    let port_text = get("rcon.port")
        .cloned()
        .unwrap_or_else(|| PyStr::from("25575"));
    let port = PyInt::parse(&port_text).map_err(|e| int_error(&port_text, &e))?;
    let password =
        get("rcon.password").ok_or_else(|| PyError::Key(PyStr::from("rcon.password")))?;
    let host = host.to_string_checked().ok_or_else(|| {
        PyError::UnicodeEncode(format!(
            "encoding with 'idna' codec failed (UnicodeEncodeError: 'idna' codec can't encode {})",
            host.repr()
        ))
    })?;
    Rcon::connect(&host, socket_port(&port)?, password, TIMEOUT)
}

/// `online_players(rcon)`: `re.search(r"There are (\d+)", reply)`, or 0.
pub fn online_players(rcon: &mut Rcon) -> Result<PyInt, PyError> {
    let reply = rcon.command(&PyStr::from("list"), None, false)?;
    let mut rest = reply.as_str();
    while let Some(i) = rest.find("There are ") {
        let after = &rest[i + "There are ".len()..];
        let digits: String = after.chars().take_while(char::is_ascii_digit).collect();
        if !digits.is_empty() {
            return Ok(PyInt::from_parts(false, &digits));
        }
        rest = after;
    }
    Ok(PyInt::from_i128(0))
}

/// `time.sleep(seconds)` for a whole number: Python refuses what doesn't fit its nanosecond clock, then negatives.
fn sleep(seconds: i128) -> Result<(), PyError> {
    const MAX: i128 = 9_223_372_036; // (2**63 - 1) ns, in whole seconds
    if !(-MAX..=MAX).contains(&seconds) {
        return Err(PyError::Overflow(
            "timestamp out of range for C PyTime_t".into(),
        ));
    }
    let secs = u64::try_from(seconds)
        .map_err(|_| PyError::Value("sleep length must be non-negative".into()))?;
    std::thread::sleep(Duration::from_secs(secs));
    Ok(())
}

/// `pid_alive(pid)`: `os.kill(pid, 0)` without the system call (std has no `kill`, and this crate has no
/// `unsafe` code to call libc): a process exists if `/proc/<pid>` does. Same answer for the server's own user, the
/// only one that runs `stop`.
fn pid_alive(pid: i128) -> Result<bool, PyError> {
    if pid > i128::from(i64::MAX) {
        return Err(PyError::Overflow(
            "Python int too large to convert to C long".into(),
        ));
    }
    if pid > i128::from(i32::MAX) {
        return Err(PyError::Overflow(
            "signed integer is greater than maximum".into(),
        ));
    }
    if pid < i128::from(i32::MIN) {
        return Err(PyError::Overflow(
            "signed integer is less than minimum".into(),
        ));
    }
    let exists = |p: &std::path::Path| match std::fs::metadata(p) {
        Ok(_) => true,
        Err(e) => e.kind() == std::io::ErrorKind::PermissionDenied, // PermissionError: alive
    };
    Ok(match pid {
        // kill(-1, 0) reaches every process we may signal, at least ourselves.
        -1 => true,
        // kill(-g, 0): is there a process in group g? Field 5 of /proc/<pid>/stat.
        p if p < 0 => process_group_exists(-p),
        p => exists(&PathBuf::from(format!("/proc/{p}"))),
    })
}

fn process_group_exists(group: i128) -> bool {
    let Ok(entries) = std::fs::read_dir("/proc") else {
        return false;
    };
    entries.flatten().any(|entry| {
        let stat = std::fs::read_to_string(entry.path().join("stat")).unwrap_or_default();
        // "pid (comm) state ppid pgrp ...": comm may hold spaces, so split after its closing parenthesis.
        stat.rsplit_once(") ")
            .and_then(|(_, rest)| rest.split(' ').nth(2))
            .and_then(|pgrp| pgrp.parse::<i128>().ok())
            == Some(group)
    })
}

/// `stop(wait_pid)`: graceful stop for systemd's ExecStop. 0 if the server exited (or was already down), else 1.
pub fn stop(wait_pid: Option<&PyInt>) -> Result<i32, PyError> {
    let start = Instant::now();
    let remaining = || STOP_BUDGET - start.elapsed().as_secs_f64();
    // A closure that returns a Result, so `?` works inside and its error is handled in one place below (Python's
    // `try: ... except (OSError, RconError)`).
    let rcon_part = || -> Result<(), PyError> {
        let mut r = connect()?;
        // save-all flush writes every chunk synchronously: allow it most of the budget.
        r.command(
            &PyStr::from("save-all flush"),
            Some((remaining() - 10.0).max(1.0)),
            false,
        )?;
        r.command(&PyStr::from("stop"), Some(remaining().max(1.0)), true)?;
        Ok(())
    };
    match rcon_part() {
        Ok(()) => {}
        Err(e) if e.is_caught() => {
            eprintln!("RCON stop failed ({e}); systemd will fall back to SIGTERM");
            return Ok(1);
        }
        Err(e) => return Err(e),
    }
    if let Some(pid) = wait_pid.filter(|p| !p.is_zero()) {
        // A pid beyond i128 is far beyond anything os.kill accepts: the same OverflowError.
        let pid = pid.to_i128().unwrap_or(i128::MAX);
        while pid_alive(pid)? && remaining() > 0.0 {
            std::thread::sleep(Duration::from_millis(500));
        }
        if pid_alive(pid)? {
            eprintln!("server still running after stop; systemd will SIGTERM it");
            return Ok(1);
        }
    }
    Ok(0)
}

/// The Discord note for a deploy restart. Python renders it with alerts.py and posts it with notify.py (HTTPS:
/// std has no TLS), so this asks the same Python code, installed next to this binary:
/// `notify.py restart-alert SECONDS ONLINE`. Without a webhook configured there is nothing to post.
fn discord_restart_alert(seconds: &PyInt, online: &PyInt) -> Result<(), PyError> {
    if env("DISCORD_WEBHOOK_URL").is_none_or(|url| url.is_empty()) {
        return Ok(());
    }
    let missing = || PyError::ModuleNotFound("No module named 'notify'".into());
    let exe = std::env::current_exe().map_err(|_| missing())?;
    let helper = exe.parent().ok_or_else(missing)?.join("notify.py");
    let status = Process::new(&helper)
        .arg("restart-alert")
        .arg(seconds.to_str()?)
        .arg(online.to_str()?)
        .status()
        .map_err(|_| missing())?;
    if status.success() {
        Ok(())
    } else {
        Err(PyError::HelperFailed)
    }
}

/// `countdown(seconds)`: announce a restart in game at 60/30/10 s. Skipped (with a message) if nobody is online
/// or the server can't be reached.
pub fn countdown(seconds: &PyInt) -> Result<(), PyError> {
    let result = (|| -> Result<(), PyError> {
        let mut r = connect()?;
        let online = online_players(&mut r)?;
        if online.is_zero() {
            say("no players online; skipping countdown");
            return Ok(());
        }
        discord_restart_alert(seconds, &online)?;
        // Arithmetic in i128: anything bigger only feeds sleep(), which refuses it either way.
        let total = seconds.to_i128().unwrap_or(if seconds.negative {
            i128::MIN
        } else {
            i128::MAX
        });
        // `[m for m in (60, 30, 10) if m <= seconds] or [seconds]`
        let mut marks: Vec<(i128, String)> = [60, 30, 10]
            .into_iter()
            .filter(|&m| m <= total)
            .map(|m| (m, m.to_string()))
            .collect();
        if marks.is_empty() {
            marks.push((total, seconds.to_str()?));
        }
        let mut remaining = total;
        for (mark, text) in marks {
            sleep(remaining.saturating_sub(mark).max(0))?;
            remaining = mark;
            r.command(
                &PyStr::from(
                    format!("say Server restarting in {text} seconds for an update.").as_str(),
                ),
                None,
                false,
            )?;
        }
        sleep(remaining)?;
        r.command(&PyStr::from("say Restarting now."), None, false)?;
        Ok(())
    })();
    match result {
        Err(e) if e.is_caught() => {
            say(&format!("countdown skipped: {e}"));
            Ok(())
        }
        other => other,
    }
}

/// `BORDER.search(reply)`: the first `(\d+(?:\.\d+)?) block` in a `worldborder get` reply.
fn border_size(reply: &str) -> Option<String> {
    let s: Vec<char> = reply.chars().collect();
    let digits_from = |i: usize| s[i..].iter().take_while(|c| c.is_ascii_digit()).count();
    let block_at =
        |i: usize| s.len() >= i + 6 && s[i..i + 6].iter().collect::<String>() == " block";
    for i in 0..s.len() {
        let int_len = digits_from(i);
        if int_len == 0 {
            continue;
        }
        let end = i + int_len;
        if s.get(end) == Some(&'.') && end + 1 < s.len() {
            let frac_len = digits_from(end + 1);
            if frac_len > 0 && block_at(end + 1 + frac_len) {
                return Some(s[i..end + 1 + frac_len].iter().collect());
            }
        }
        if block_at(end) {
            return Some(s[i..end].iter().collect());
        }
    }
    None
}

/// `round(float(size)) == want`. Python's `round` rounds halves to even, and the float's integer is exact.
fn border_is(size: &str, want: &PyInt) -> Result<bool, PyError> {
    let value: f64 = size.parse().unwrap_or(f64::INFINITY);
    let rounded = value.round_ties_even();
    if !rounded.is_finite() {
        return Err(PyError::Overflow(
            "cannot convert float infinity to integer".into(),
        ));
    }
    Ok(PyInt::from_parts(false, &format!("{rounded:.0}")) == *want)
}

/// `"execute in {dimension} run " + rest`.
fn in_dimension(dimension: &PyStr, rest: &str) -> PyStr {
    let mut cmd = PyStr::from("execute in ");
    cmd.push_py(dimension);
    cmd.push_str(" run ");
    cmd.push_str(rest);
    cmd
}

/// `worldborder(dimension, radius)`: a border of 2 * radius centred on 0,0. `Ok(true)` if it changed.
pub fn worldborder(dimension: &PyStr, radius: &PyInt) -> Result<bool, PyError> {
    let want = radius.doubled();
    let mut r = connect()?;
    let get = in_dimension(dimension, "worldborder get");
    if let Some(size) = border_size(&r.command(&get, None, false)?)
        && border_is(&size, &want)?
    {
        return Ok(false);
    }
    r.command(
        &in_dimension(dimension, "worldborder center 0 0"),
        None,
        false,
    )?;
    let want_text = want.to_str()?;
    r.command(
        &in_dimension(dimension, &format!("worldborder set {want_text}")),
        None,
        false,
    )?;
    let ok = match border_size(&r.command(&get, None, false)?) {
        Some(size) => border_is(&size, &want)?,
        None => false,
    };
    if !ok {
        return Err(PyError::Rcon(format!(
            "failed to set {} border to {want_text}",
            dimension.display()
        )));
    }
    Ok(true)
}

/// `GAMERULE_VALUE.search(reply)`: `(?:currently|now|already) set to:? (\S+)\s*$` — the value after the first
/// "currently/now/already set to" that is followed only by whitespace.
fn gamerule_value(reply: &[u32]) -> Option<PyStr> {
    let text = PyStr::from_code_points(reply);
    let cps = text.code_points();
    for start in 0..cps.len() {
        for word in ["currently", "now", "already"] {
            let lead: Vec<u32> = format!("{word} set to").chars().map(u32::from).collect();
            if !cps[start..].starts_with(&lead) {
                continue;
            }
            let mut j = start + lead.len();
            if cps.get(j) == Some(&u32::from(':')) {
                j += 1;
            }
            if cps.get(j) != Some(&u32::from(' ')) {
                continue;
            }
            j += 1;
            let value_len = cps[j..]
                .iter()
                .take_while(|&&c| !crate::pystr::is_space(c))
                .count();
            if value_len > 0
                && cps[j + value_len..]
                    .iter()
                    .all(|&c| crate::pystr::is_space(c))
            {
                return Some(PyStr::from_code_points(&cps[j..j + value_len]));
            }
        }
    }
    None
}

/// `GAMERULE_NAME.match(name)`: `^[a-z][a-z0-9_]{0,63}$` (`$` also before one final newline).
fn valid_rule_name(name: &PyStr) -> bool {
    let mut cps = name.code_points();
    if cps.last() == Some(&u32::from('\n')) {
        cps = &cps[..cps.len() - 1];
    }
    let ok = |c: u32, first: bool| {
        char::from_u32(c)
            .is_some_and(|c| c.is_ascii_lowercase() || (!first && (c.is_ascii_digit() || c == '_')))
    };
    (1..=64).contains(&cps.len()) && ok(cps[0], true) && cps[1..].iter().all(|&c| ok(c, false))
}

/// `re.fullmatch(r"true|false|-?\d{1,9}", value)`.
fn valid_rule_value(value: &PyStr) -> bool {
    if value.eq_str("true") || value.eq_str("false") {
        return true;
    }
    let cps = value.code_points();
    let digits = if cps.first() == Some(&u32::from('-')) {
        &cps[1..]
    } else {
        cps
    };
    (1..=9).contains(&digits.len())
        && digits
            .iter()
            .all(|&c| char::from_u32(c).is_some_and(|c| c.is_ascii_digit()))
}

/// `gamerule(name, value, dimensions)`: set a game rule in every dimension, reading first and setting only a
/// different value; a dimension the server doesn't have is skipped. `Ok(true)` if anything changed.
pub fn gamerule(name: &PyStr, value: &PyStr, dimensions: &[PyStr]) -> Result<bool, PyError> {
    if !valid_rule_name(name) || !valid_rule_value(value) {
        return Err(PyError::Rcon(format!(
            "bad game rule {} = {}",
            name.repr(),
            value.repr()
        )));
    }
    let mut read = PyStr::from("gamerule ");
    read.push_py(name);
    let mut write = read.clone();
    write.push_str(" ");
    write.push_py(value);
    let mut changed = false;
    let mut r = connect()?;
    for dim in dimensions {
        let with_prefix = |cmd: &PyStr| {
            let mut full = in_dimension(dim, "");
            full.push_py(cmd);
            full
        };
        let reply = r.command(&with_prefix(&read), None, false)?;
        if reply.contains("Unknown dimension") {
            continue;
        }
        let reply_cps: Vec<u32> = reply.chars().map(u32::from).collect();
        let Some(current) = gamerule_value(&reply_cps) else {
            let head = PyStr::from_code_points(&reply_cps[..reply_cps.len().min(200)]);
            return Err(PyError::Rcon(format!(
                "gamerule {} in {}: unexpected reply {}",
                name.display(),
                dim.display(),
                head.repr()
            )));
        };
        if current == *value {
            continue;
        }
        r.command(&with_prefix(&write), None, false)?;
        let again: Vec<u32> = r
            .command(&with_prefix(&read), None, false)?
            .chars()
            .map(u32::from)
            .collect();
        if gamerule_value(&again).as_ref() != Some(value) {
            return Err(PyError::Rcon(format!(
                "failed to set {} to {} in {}",
                name.display(),
                value.display(),
                dim.display()
            )));
        }
        changed = true;
    }
    Ok(changed)
}

/// `cmd COMMAND...`: the reply of one command.
pub fn cmd(words: &[PyStr]) -> Result<String, PyError> {
    let mut r = connect()?;
    r.command(&PyStr::join(words, " "), None, false)
}

/// `$MAINPID` for stop: `int(pid) if pid else None`.
pub fn main_pid() -> Result<Option<PyInt>, PyError> {
    match env("MAINPID") {
        Some(text) if !text.is_empty() => PyInt::parse(&text)
            .map(Some)
            .map_err(|e| int_error(&text, &e)),
        _ => Ok(None),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn paths_like_pathlib() {
        let j = |d: &str| path_join(&PyStr::from(d), "server.properties").display();
        assert_eq!(
            j("/opt/minecraft/server"),
            "/opt/minecraft/server/server.properties"
        );
        assert_eq!(j(""), "server.properties");
        assert_eq!(j("//tmp/x/"), "//tmp/x/server.properties");
        assert_eq!(j("///tmp/./x//"), "/tmp/x/server.properties");
        assert_eq!(j("a/../b"), "a/../b/server.properties");
    }

    #[test]
    fn ports_like_getaddrinfo() {
        let p = |n: i128| socket_port(&PyInt::from_i128(n)).map_err(|e| e.to_string());
        assert_eq!(p(25575), Ok(25575));
        assert_eq!(p(65536 + 80), Ok(80));
        assert_eq!(
            p(-1),
            Err("[Errno -8] Servname not supported for ai_socktype".into())
        );
    }

    #[test]
    fn border_parsing_and_rounding() {
        assert_eq!(
            border_size("The world border is currently 10000 block(s) wide"),
            Some("10000".into())
        );
        assert_eq!(
            border_size("now 59999968.0 blocks"),
            Some("59999968.0".into())
        );
        assert_eq!(border_size("1.5.3 block"), Some("5.3".into()));
        assert_eq!(border_size("no number"), None);
        let want = PyInt::from_i128(10000);
        assert!(border_is("10000.4", &want).unwrap());
        assert!(border_is("10000.5", &want).unwrap()); // halves to even, as Python's round()
        assert!(!border_is("10001.5", &want).unwrap());
        assert!(border_is(&"9".repeat(400), &want).is_err());
    }

    #[test]
    fn gamerule_replies() {
        let v = |s: &str| {
            gamerule_value(&s.chars().map(u32::from).collect::<Vec<_>>()).map(|p| p.display())
        };
        assert_eq!(
            v("Gamerule locator_bar is currently set to: true"),
            Some("true".into())
        );
        assert_eq!(v("Gamerule x is now set to: 50 \n"), Some("50".into()));
        assert_eq!(v("already set to 3"), Some("3".into()));
        assert_eq!(v("snow set to: x"), Some("x".into()));
        assert_eq!(v("currently set to: a b"), None);
        assert_eq!(v("currently set to:x"), None);
        assert!(valid_rule_name(&PyStr::from("locator_bar")));
        assert!(valid_rule_name(&PyStr::from("abc\n")));
        assert!(!valid_rule_name(&PyStr::from("Abc")));
        assert!(!valid_rule_name(&PyStr::from(&*"a".repeat(65))));
        assert!(valid_rule_value(&PyStr::from("-123456789")));
        assert!(!valid_rule_value(&PyStr::from("1234567890")));
        assert!(!valid_rule_value(&PyStr::from("true\n")));
    }

    #[test]
    fn sleep_limits() {
        assert_eq!(
            sleep(-1).unwrap_err().traceback_line(),
            "ValueError: sleep length must be non-negative"
        );
        assert_eq!(
            sleep(9_223_372_037).unwrap_err().traceback_line(),
            "OverflowError: timestamp out of range for C PyTime_t"
        );
        assert!(sleep(0).is_ok());
    }

    #[test]
    fn our_own_process_is_alive() {
        assert!(pid_alive(i128::from(std::process::id())).unwrap());
        assert!(pid_alive(-1).unwrap());
        assert!(pid_alive(i128::from(i32::MAX) + 1).is_err());
    }
}
