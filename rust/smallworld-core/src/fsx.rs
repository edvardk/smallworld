//! State files the way the Python scripts write and read them.
//!
//! - `atomic_write` is mc_panel.py's `atomic_write`, step for step: `<path>.tmp` opened with
//!   `O_WRONLY|O_CREAT|O_TRUNC|O_CLOEXEC` and mode 0600 (so the umask can only narrow it), `fchmod(0600)` (an
//!   existing tmp file can't keep wider bits while it is written), write, `fchmod(mode)`, then `rename` over the
//!   target. A reader sees the old file or the new one, never half of one or a wider mode mid-write.
//! - `replace_write` is the plainer form notify.py and mc_agent.py's `_stop_now` use: `tmp.write_text(text)`
//!   (mode from the umask, 0666 & ~umask for a new file), optionally `tmp.chmod(mode)`, then `os.replace`.

use std::fs::{self, OpenOptions};
use std::io::{self, Write};
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};

/// `path.with_name(path.name + ".tmp")`.
pub fn tmp_path(path: &Path) -> PathBuf {
    let mut name = path.file_name().unwrap_or_default().to_os_string();
    name.push(".tmp");
    path.with_file_name(name)
}

/// mc_panel.py `atomic_write(path, text, mode)`.
///
/// # Errors
/// Any `OSError` of the steps (the tmp file may then be left behind, as in Python).
pub fn atomic_write(path: &Path, data: &[u8], mode: u32) -> io::Result<()> {
    let tmp = tmp_path(path);
    let mut f = OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .mode(0o600)
        .open(&tmp)?; // std adds O_CLOEXEC
    f.set_permissions(fs::Permissions::from_mode(0o600))?;
    f.write_all(data)?;
    f.flush()?;
    f.set_permissions(fs::Permissions::from_mode(mode))?;
    drop(f);
    fs::rename(&tmp, path)
}

/// `tmp.write_text(text)`, then `tmp.chmod(mode)` if given, then `os.replace(tmp, path)` (notify.py's maintenance
/// marker: no chmod; mc_agent.py's admin-stopped marker: 0644).
///
/// # Errors
/// Any `OSError` of the steps.
pub fn replace_write(path: &Path, data: &[u8], mode: Option<u32>) -> io::Result<()> {
    let tmp = tmp_path(path);
    fs::write(&tmp, data)?;
    if let Some(m) = mode {
        fs::set_permissions(&tmp, fs::Permissions::from_mode(m))?;
    }
    fs::rename(&tmp, path)
}

/// `path.unlink(missing_ok=True)`.
///
/// # Errors
/// Any error but "not found".
pub fn unlink_missing_ok(path: &Path) -> io::Result<()> {
    match fs::remove_file(path) {
        Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(()),
        other => other,
    }
}

/// `Path.exists()`: true when `stat` succeeds (follows symlinks; any error, permission included, is false).
pub fn exists(path: &Path) -> bool {
    fs::metadata(path).is_ok()
}

/// `path.read_text()` with Python's strict UTF-8 decoding: `None` on any error (`OSError` or
/// `UnicodeDecodeError`), which is how every caller here treats it (`except (OSError, ValueError)`).
pub fn read_text(path: &Path) -> Option<String> {
    String::from_utf8(fs::read(path).ok()?).ok()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::MetadataExt;

    fn tmpdir(tag: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("swcore-fsx-{tag}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&d);
        fs::create_dir_all(&d).unwrap();
        d
    }

    #[test]
    fn atomic_write_sets_the_mode_and_replaces() {
        let d = tmpdir("aw");
        let p = d.join("f.json");
        fs::write(&p, "old").unwrap();
        // A stale tmp with wide bits must not keep them.
        fs::write(tmp_path(&p), "stale").unwrap();
        fs::set_permissions(tmp_path(&p), fs::Permissions::from_mode(0o666)).unwrap();
        atomic_write(&p, b"new", 0o640).unwrap();
        assert_eq!(fs::read_to_string(&p).unwrap(), "new");
        assert_eq!(fs::metadata(&p).unwrap().mode() & 0o7777, 0o640);
        assert!(!tmp_path(&p).exists());
        atomic_write(&p, b"x", 0o600).unwrap();
        assert_eq!(fs::metadata(&p).unwrap().mode() & 0o7777, 0o600);
        fs::remove_dir_all(d).unwrap();
    }

    #[test]
    fn replace_write_and_unlink() {
        let d = tmpdir("rw");
        let p = d.join("admin-stopped");
        replace_write(&p, b"{}\n", Some(0o644)).unwrap();
        assert_eq!(fs::metadata(&p).unwrap().mode() & 0o7777, 0o644);
        assert!(exists(&p));
        unlink_missing_ok(&p).unwrap();
        unlink_missing_ok(&p).unwrap();
        assert!(!exists(&p));
        assert_eq!(
            tmp_path(Path::new("/a/b.json")),
            PathBuf::from("/a/b.json.tmp")
        );
        fs::write(&p, b"\xff").unwrap();
        assert_eq!(read_text(&p), None);
        fs::remove_dir_all(d).unwrap();
    }
}
