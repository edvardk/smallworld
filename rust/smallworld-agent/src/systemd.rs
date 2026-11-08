//! systemctl and journalctl, as mc_agent.py/mc_panel.py call them: `systemctl --no-ask-password VERB UNIT` on the
//! polkit-allowed unit/verb pairs only (ansible/roles/base/templates/50-mc-ops.rules.j2), and read-only queries.
//! No D-Bus, no root. The binaries are looked up on `PATH` like Python's `subprocess` (the parity harness puts
//! fakes first).

use crate::types::{AResult, AgentError};
use std::io::Read;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

/// Runs a command with a timeout like `subprocess.run(argv, timeout=..., capture_output=True)`: the child is
/// killed on timeout (`TimeoutExpired`); returns the exit code and stdout. A missing binary is the `OSError`
/// Python raises (`FileNotFoundError`).
fn run(argv: &[&str], timeout: Duration) -> AResult<(Option<i32>, Vec<u8>)> {
    let mut child = Command::new(argv[0])
        .args(&argv[1..])
        .stdin(Stdio::inherit())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()?;
    let mut stdout = child.stdout.take();
    // Drain stdout on a thread so a chatty child never blocks on a full pipe.
    let reader = std::thread::spawn(move || {
        let mut buf = Vec::new();
        if let Some(out) = stdout.as_mut() {
            let _ = out.read_to_end(&mut buf);
        }
        buf
    });
    let deadline = Instant::now() + timeout;
    let status = loop {
        if let Some(st) = child.try_wait()? {
            break st;
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            let _ = reader.join();
            return Err(AgentError::Os("TimeoutExpired".into()));
        }
        std::thread::sleep(Duration::from_millis(20));
    };
    let out = reader.join().unwrap_or_default();
    Ok((status.code(), out))
}

/// mc_agent.py `systemctl(*args, timeout=300)`: `check=True`, so a non-zero exit is `CalledProcessError`.
///
/// # Errors
/// `Os("CalledProcessError")`, `Os("TimeoutExpired")`, or the spawn's `OSError` class.
pub fn systemctl(args: &[&str], timeout_s: f64) -> AResult<()> {
    let mut argv = vec!["systemctl", "--no-ask-password"];
    argv.extend_from_slice(args);
    let (code, _) = run(&argv, Duration::from_secs_f64(timeout_s))?;
    if code == Some(0) {
        Ok(())
    } else {
        Err(AgentError::Os("CalledProcessError".into()))
    }
}

/// mc_panel.py `unit_steps(unit, since)`: the `STEP `/`RESTORED`/`FAILED` lines a unit printed since `since`
/// (local time, `journalctl --since`). `text=True`: a reply that isn't UTF-8 is a `UnicodeDecodeError`.
///
/// # Errors
/// The `OSError`/`TimeoutExpired` of running journalctl, or `Value("UnicodeDecodeError")`.
pub fn unit_steps(unit: &str, since: &str) -> AResult<Vec<String>> {
    let argv = [
        "journalctl",
        "-u",
        unit,
        "--since",
        since,
        "-o",
        "cat",
        "--no-pager",
        "-n",
        "200",
    ];
    let (_, out) = run(&argv, Duration::from_secs(30))?;
    let text = String::from_utf8(out).map_err(|_| {
        AgentError::Value(
            "UnicodeDecodeError".into(),
            "'utf-8' codec can't decode".into(),
        )
    })?;
    Ok(splitlines(&text)
        .into_iter()
        .filter(|l| l.starts_with("STEP ") || l.starts_with("RESTORED") || l.starts_with("FAILED"))
        .map(str::to_string)
        .collect())
}

/// `str.splitlines()` (Python's line boundaries, `\r\n` as one).
pub fn splitlines(text: &str) -> Vec<&str> {
    let mut out = Vec::new();
    let mut start = 0;
    let mut it = text.char_indices().peekable();
    while let Some((i, c)) = it.next() {
        if matches!(
            c,
            '\n' | '\r'
                | '\u{0b}'
                | '\u{0c}'
                | '\u{1c}'
                | '\u{1d}'
                | '\u{1e}'
                | '\u{85}'
                | '\u{2028}'
                | '\u{2029}'
        ) {
            out.push(&text[start..i]);
            let mut end = i + c.len_utf8();
            if c == '\r' && it.peek().is_some_and(|&(_, n)| n == '\n') {
                it.next();
                end += 1;
            }
            start = end;
        }
    }
    if start < text.len() {
        out.push(&text[start..]);
    }
    out
}

/// mc_agent.py `running_units()` (owner: data): which heavy jobs run now (backup, restore, prune).
pub fn running_units() -> Vec<String> {
    let names = [
        ("mc-backup.service", "backup"),
        ("mc-restore-apply.service", "restore"),
        ("mc-prune.service", "prune"),
    ];
    let mut argv = vec!["systemctl", "is-active"];
    argv.extend(names.iter().map(|(u, _)| *u));
    let Ok((_, out)) = run(&argv, Duration::from_secs(10)) else {
        return Vec::new();
    };
    let text = String::from_utf8_lossy(&out);
    names
        .iter()
        .zip(smallworld_core::pystr::split_ws(&text))
        .filter(|(_, st)| matches!(*st, "active" | "activating"))
        .map(|((_, label), _)| (*label).to_string())
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn splitlines_like_python() {
        assert_eq!(
            splitlines("a\nb\r\nc\rd\u{2028}e"),
            vec!["a", "b", "c", "d", "e"]
        );
        assert_eq!(splitlines("a\n\nb\n"), vec!["a", "", "b"]);
        assert!(splitlines("").is_empty());
    }

    #[test]
    fn run_reports_python_classes() {
        assert_eq!(
            run(&["/nonexistent/x"], Duration::from_secs(1)).unwrap_err(),
            AgentError::Os("FileNotFoundError".into())
        );
        assert_eq!(
            run(&["sleep", "5"], Duration::from_millis(100)).unwrap_err(),
            AgentError::Os("TimeoutExpired".into())
        );
        let (code, out) = run(&["sh", "-c", "echo hi; exit 3"], Duration::from_secs(5)).unwrap();
        assert_eq!((code, out), (Some(3), b"hi\n".to_vec()));
    }
}
