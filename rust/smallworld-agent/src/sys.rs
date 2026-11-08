//! The few things std lacks, from libc: `umask`, `time.monotonic`, `shutil.disk_usage` (statvfs), local time (`localtime_r`, for `time.strftime`) and signals
//! (`pthread_sigmask` + `sigwait` on a dedicated thread, Python's `signal.signal` handlers without async-signal
//! code). Every `unsafe` block in the crate is here, each with what makes it sound.

#![allow(unsafe_code)]

use std::time::{SystemTime, UNIX_EPOCH};

/// `os.umask(mode)`; returns the previous mask.
pub fn umask(mode: u32) -> u32 {
    // SAFETY: umask(2) only swaps the process's file mode creation mask; it can't fail and touches no memory.
    unsafe { libc::umask(mode as libc::mode_t) as u32 }
}

/// `time.time()`.
pub fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0.0, |d| d.as_secs_f64())
}

/// `time.monotonic()`: CLOCK_MONOTONIC in seconds (the clock systemd's `*TimestampMonotonic` properties use;
/// std's `Instant` can't be compared with them).
pub fn monotonic() -> f64 {
    let mut ts = libc::timespec {
        tv_sec: 0,
        tv_nsec: 0,
    };
    // SAFETY: clock_gettime writes one timespec through a valid pointer to a local; CLOCK_MONOTONIC always exists.
    let rc = unsafe { libc::clock_gettime(libc::CLOCK_MONOTONIC, &raw mut ts) };
    if rc != 0 {
        return 0.0;
    }
    #[allow(clippy::cast_precision_loss)]
    let secs = ts.tv_sec as f64 + ts.tv_nsec as f64 / 1e9;
    secs
}

/// `shutil.disk_usage(path)`: (total, used, free) bytes from statvfs (used = blocks - bfree, free = bavail).
///
/// # Errors
/// The `OSError` of statvfs.
pub fn disk_usage(path: &std::path::Path) -> std::io::Result<(u64, u64, u64)> {
    use std::os::unix::ffi::OsStrExt;
    let c = std::ffi::CString::new(path.as_os_str().as_bytes())
        .map_err(|_| std::io::Error::from_raw_os_error(libc::EINVAL))?;
    // SAFETY: an all-zero statvfs is a valid value of this plain C struct; statvfs fills it.
    let mut st: libc::statvfs = unsafe { std::mem::zeroed() };
    // SAFETY: `c` is a NUL-terminated string that outlives the call; `st` is a valid, writable statvfs.
    let rc = unsafe { libc::statvfs(c.as_ptr(), &raw mut st) };
    if rc != 0 {
        return Err(std::io::Error::last_os_error());
    }
    #[allow(clippy::unnecessary_cast)]
    let (blocks, bfree, bavail, frsize) = (
        st.f_blocks as u64,
        st.f_bfree as u64,
        st.f_bavail as u64,
        st.f_frsize as u64,
    );
    Ok((
        blocks * frsize,
        blocks.saturating_sub(bfree) * frsize,
        bavail * frsize,
    ))
}

/// Broken-down time fields we print.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Tm {
    pub year: i64,
    pub month: u32,
    pub day: u32,
    pub hour: u32,
    pub minute: u32,
    pub second: u32,
}

/// Days since 1970-01-01 -> (year, month, day), proleptic Gregorian (Howard Hinnant's civil_from_days).
fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = u32::try_from(doy - (153 * mp + 2) / 5 + 1).unwrap_or(1);
    let m = u32::try_from(if mp < 10 { mp + 3 } else { mp - 9 }).unwrap_or(1);
    (if m <= 2 { y + 1 } else { y }, m, d)
}

/// `time.gmtime(t)` (whole seconds, floor).
pub fn gmtime(t: f64) -> Tm {
    #[allow(clippy::cast_possible_truncation)]
    let secs = t.floor() as i64;
    let (year, month, day) = civil_from_days(secs.div_euclid(86_400));
    let rem = secs.rem_euclid(86_400);
    let r = u32::try_from(rem).unwrap_or(0);
    Tm {
        year,
        month,
        day,
        hour: r / 3600,
        minute: r % 3600 / 60,
        second: r % 60,
    }
}

/// `time.localtime(t)` through the C library (musl reads `TZ` and /etc/localtime, like glibc for Python).
pub fn localtime(t: f64) -> Tm {
    // libc 0.2 marks musl's time_t deprecated (it will follow musl 1.2's 64-bit time_t); on our 64-bit targets
    // it already is 64-bit, so the alias is exactly what localtime_r takes.
    #[allow(clippy::cast_possible_truncation, deprecated)]
    let secs = t.floor() as libc::time_t;
    // SAFETY: `tm` is a plain C struct, valid when zeroed; localtime_r writes only into it and reads `secs`.
    let mut tm: libc::tm = unsafe { std::mem::zeroed() };
    // SAFETY: both pointers are to live locals of the right types; localtime_r is the thread-safe variant.
    let ok = unsafe { !libc::localtime_r(&raw const secs, &raw mut tm).is_null() };
    if !ok {
        return gmtime(t);
    }
    Tm {
        year: i64::from(tm.tm_year) + 1900,
        month: u32::try_from(tm.tm_mon + 1).unwrap_or(1),
        day: u32::try_from(tm.tm_mday).unwrap_or(1),
        hour: u32::try_from(tm.tm_hour).unwrap_or(0),
        minute: u32::try_from(tm.tm_min).unwrap_or(0),
        second: u32::try_from(tm.tm_sec).unwrap_or(0),
    }
}

/// `time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())`.
pub fn utc_iso(t: f64) -> String {
    let g = gmtime(t);
    format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}Z",
        g.year, g.month, g.day, g.hour, g.minute, g.second
    )
}

/// `time.strftime("%Y-%m-%d %H:%M:%S")` (local time): `journalctl --since`.
pub fn local_since(t: f64) -> String {
    let l = localtime(t);
    format!(
        "{:04}-{:02}-{:02} {:02}:{:02}:{:02}",
        l.year, l.month, l.day, l.hour, l.minute, l.second
    )
}

/// The signals `cli_main` handles (SIGTERM, SIGHUP, SIGINT) and the daemon's (SIGTERM, SIGINT).
pub const CLI_SIGNALS: [libc::c_int; 3] = [libc::SIGTERM, libc::SIGHUP, libc::SIGINT];
pub const DAEMON_SIGNALS: [libc::c_int; 2] = [libc::SIGTERM, libc::SIGINT];

fn sigset(signals: &[libc::c_int]) -> libc::sigset_t {
    // SAFETY: sigset_t is a plain C bitmask; sigemptyset initialises it before sigaddset touches it.
    let mut set: libc::sigset_t = unsafe { std::mem::zeroed() };
    // SAFETY: `set` is a live local; the signal numbers are valid constants.
    unsafe {
        libc::sigemptyset(&raw mut set);
        for &s in signals {
            libc::sigaddset(&raw mut set, s);
        }
    }
    set
}

/// `signal.signal(sig, handler)` for each of `signals`, the safe way: the signals are blocked in this thread
/// (and so in every thread started after this call, which inherit the mask) and a dedicated thread takes them
/// with `sigwait` and calls `handler(signal)` there. Call it before starting other threads. Children started
/// with std's `Command` get a cleared mask (std resets it before exec), so `systemctl` and `journalctl` behave
/// as under Python.
pub fn handle_signals(signals: &[libc::c_int], handler: impl Fn(libc::c_int) + Send + 'static) {
    let set = sigset(signals);
    // SAFETY: `set` is initialised; a null old-mask pointer is allowed.
    let rc =
        unsafe { libc::pthread_sigmask(libc::SIG_BLOCK, &raw const set, std::ptr::null_mut()) };
    if rc != 0 {
        return; // Python: `contextlib.suppress(ValueError, OSError)` around signal.signal
    }
    let spawned = std::thread::Builder::new()
        .name("signals".into())
        .spawn(move || {
            loop {
                let mut sig: libc::c_int = 0;
                // SAFETY: `set` was initialised above and is moved into this thread; `sig` is a live local.
                let rc = unsafe { libc::sigwait(&raw const set, &raw mut sig) };
                if rc == 0 {
                    handler(sig);
                }
            }
        });
    if spawned.is_err() {
        // Without the thread the signals would stay blocked forever: restore the default delivery.
        // SAFETY: as above.
        unsafe { libc::pthread_sigmask(libc::SIG_UNBLOCK, &raw const set, std::ptr::null_mut()) };
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn gmtime_matches_python() {
        // time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(1762560000.9)) == "2025-11-08T00:00:00Z"
        assert_eq!(utc_iso(1_762_560_000.9), "2025-11-08T00:00:00Z");
        assert_eq!(utc_iso(0.0), "1970-01-01T00:00:00Z");
        assert_eq!(utc_iso(951_825_600.0), "2000-02-29T12:00:00Z");
        assert_eq!(utc_iso(4_102_444_799.0), "2099-12-31T23:59:59Z");
    }

    #[test]
    fn umask_round_trips() {
        let old = umask(0o077);
        assert_eq!(umask(old), 0o077);
    }
}
