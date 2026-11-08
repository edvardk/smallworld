//! flock(2) locks that interlock with Python's `fcntl.flock` on the same files: the job lock
//! (mc_panel.py `Jobs._take_file_lock`, `JOB_LOCK_FILE`) and pending.json's lock (`Pending.locked`).
//!
//! std's `File::lock`/`File::try_lock` are flock(2) on Linux (`LOCK_EX`, `LOCK_EX|LOCK_NB`), so no `unsafe` and
//! no libc are needed. The kernel drops the lock when the descriptor is closed, so a process that dies holds
//! nothing; dropping the guard closes the file (Python: `flock(LOCK_UN)` then `os.close`).

use crate::errno::io_class;
use std::fs::{File, OpenOptions};
use std::io;
use std::os::unix::fs::OpenOptionsExt;
use std::path::Path;
use std::sync::{Mutex, MutexGuard};

/// The text `Jobs._take_file_lock` refuses with (mc_panel.py `ActionError`), when the other process holds it.
pub const JOB_BUSY: &str =
    "busy: another action is running (started from the panel or mcctl); wait, or cancel it there";

/// `os.open(path, O_RDWR | O_CREAT [| O_CLOEXEC], 0o600)` (Python's descriptors are close-on-exec anyway).
fn open_lock_file(path: &Path) -> io::Result<File> {
    OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .open(path)
}

/// A held job lock (the open lock file); dropping it releases the lock.
#[derive(Debug)]
pub struct JobLockGuard {
    _file: Option<File>,
}

/// Why the job lock wasn't taken.
#[derive(Debug, PartialEq, Eq)]
pub enum JobLockError {
    /// Another process holds it: the job must not start (`ActionError(JOB_BUSY)`).
    Busy,
}

/// `Jobs._take_file_lock`: `flock(LOCK_EX | LOCK_NB)` on `path`. A lock file that can't be opened (no state dir:
/// a test, a dev box) only loses the cross-process check: a line on stderr and a guard that holds nothing.
///
/// # Errors
/// `JobLockError::Busy` when the lock is held elsewhere.
pub fn take_job_lock(path: &Path) -> Result<JobLockGuard, JobLockError> {
    let file = match open_lock_file(path) {
        Ok(f) => f,
        Err(e) => {
            eprintln!(
                "job lock {}: {} (one job per process only)",
                path.display(),
                io_class(&e)
            );
            return Ok(JobLockGuard { _file: None });
        }
    };
    // Python treats any OSError of flock here as "held elsewhere".
    match file.try_lock() {
        Ok(()) => Ok(JobLockGuard { _file: Some(file) }),
        Err(_) => Err(JobLockError::Busy),
    }
}

/// pending.json's lock: a mutex for the threads of this process plus a blocking flock on `<pending>.lock` for the
/// other process (mc_panel.py `Pending.lock` + `Pending.locked`). Held only around a read and a write of a small
/// file, never across a network call.
#[derive(Debug)]
pub struct ProcessFileLock {
    path: std::path::PathBuf,
    threads: Mutex<()>,
}

/// A held `ProcessFileLock`; dropping it releases both locks (the file first, then the mutex).
pub struct ProcessFileLockGuard<'a> {
    _file: Option<File>,
    _threads: MutexGuard<'a, ()>,
}

impl ProcessFileLock {
    pub fn new(path: impl Into<std::path::PathBuf>) -> Self {
        Self {
            path: path.into(),
            threads: Mutex::new(()),
        }
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    /// `Pending.locked()`: blocks until both locks are held. A lock file that can't be opened only loses the
    /// cross-process part (a line on stderr, as Python prints it).
    pub fn lock(&self) -> ProcessFileLockGuard<'_> {
        // A thread that panicked while holding the mutex left nothing half-done that the flock protects (the
        // file write is atomic): carry on, like Python, which has no poisoning.
        let threads = self
            .threads
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let file = match open_lock_file(&self.path) {
            Ok(f) => {
                // `fcntl.flock(fd, LOCK_EX)`: an error here propagates in Python (it never happens on a local
                // file); keep the descriptor without the lock rather than fail the caller.
                if let Err(e) = f.lock() {
                    eprintln!(
                        "pending lock {}: {} (this process only)",
                        self.path.display(),
                        io_class(&e)
                    );
                }
                Some(f)
            }
            Err(e) => {
                eprintln!(
                    "pending lock {}: {} (this process only)",
                    self.path.display(),
                    io_class(&e)
                );
                None
            }
        };
        ProcessFileLockGuard {
            _file: file,
            _threads: threads,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::process::Command;

    fn tmp(name: &str) -> std::path::PathBuf {
        let d = std::env::temp_dir().join(format!("swcore-flock-{}", std::process::id()));
        std::fs::create_dir_all(&d).unwrap();
        d.join(name)
    }

    #[test]
    fn job_lock_is_exclusive_and_released_on_drop() {
        let p = tmp("job.lock");
        let held = take_job_lock(&p).unwrap();
        assert_eq!(take_job_lock(&p).unwrap_err(), JobLockError::Busy);
        drop(held);
        let again = take_job_lock(&p).unwrap();
        drop(again);
    }

    #[test]
    fn job_lock_interlocks_with_python_flock() {
        // Skipped quietly where there is no python3 (the CI rust job has one).
        let p = tmp("py-job.lock");
        let script = format!(
            "import fcntl,os,sys\nfd=os.open({:?},os.O_RDWR|os.O_CREAT,0o600)\n\
             try:\n fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)\nexcept OSError:\n sys.exit(3)\nsys.exit(0)",
            p.display().to_string()
        );
        let held = take_job_lock(&p).unwrap();
        let Ok(status) = Command::new("python3").arg("-c").arg(&script).status() else {
            return;
        };
        assert_eq!(status.code(), Some(3), "Python got the lock Rust holds");
        drop(held);
        let status = Command::new("python3")
            .arg("-c")
            .arg(&script)
            .status()
            .unwrap();
        assert_eq!(status.code(), Some(0));
    }

    #[test]
    fn unopenable_lock_file_degrades_to_this_process() {
        let p = std::path::Path::new("/nonexistent-dir-swcore/job.lock");
        assert!(take_job_lock(p).is_ok());
        let l = ProcessFileLock::new("/nonexistent-dir-swcore/pending.json.lock");
        drop(l.lock());
    }

    #[test]
    fn process_lock_serialises_threads() {
        let l = std::sync::Arc::new(ProcessFileLock::new(tmp("pending.json.lock")));
        let counter = std::sync::Arc::new(Mutex::new(Vec::new()));
        let handles: Vec<_> = (0..4)
            .map(|i| {
                let (l, c) = (l.clone(), counter.clone());
                std::thread::spawn(move || {
                    let _g = l.lock();
                    c.lock().unwrap().push(i);
                    std::thread::sleep(std::time::Duration::from_millis(5));
                    c.lock().unwrap().push(i);
                })
            })
            .collect();
        for h in handles {
            h.join().unwrap();
        }
        let v = counter.lock().unwrap();
        for pair in v.chunks(2) {
            assert_eq!(pair[0], pair[1], "two holders at once: {v:?}");
        }
    }
}
