//! Test helper (std only, compiled for tests alone): a temporary directory that is removed when its guard
//! drops, also when the test panics (as smallworld-map's `tests/common` `TmpDir`), so `cargo test` leaves
//! nothing in TMPDIR.
use std::ops::Deref;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU32, Ordering};

static NEXT: AtomicU32 = AtomicU32::new(0);

/// Owns `<TMPDIR>/<prefix>-<pid>-<n>` (unique per call, so tests running in parallel never share one) and
/// derefs to a path in it: the directory itself, or a file in it after `child`.
pub struct TmpDir {
    root: PathBuf,
    path: PathBuf,
}

impl TmpDir {
    /// A fresh, empty directory.
    pub fn new(prefix: &str) -> TmpDir {
        let n = NEXT.fetch_add(1, Ordering::Relaxed);
        let root = std::env::temp_dir().join(format!("{prefix}-{}-{n}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();
        TmpDir {
            path: root.clone(),
            root,
        }
    }

    /// The same guard, now deref'ing to `name` inside the directory (still removing the whole directory).
    pub fn child(mut self, name: &str) -> TmpDir {
        self.path = self.root.join(name);
        self
    }
}

impl Deref for TmpDir {
    type Target = PathBuf;
    fn deref(&self) -> &PathBuf {
        &self.path
    }
}

impl AsRef<Path> for TmpDir {
    fn as_ref(&self) -> &Path {
        &self.path
    }
}

/// For constructors taking `impl Into<PathBuf>` (the guard keeps owning the directory).
impl From<&TmpDir> for PathBuf {
    fn from(d: &TmpDir) -> PathBuf {
        d.path.clone()
    }
}

impl Drop for TmpDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.root);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn removed_on_drop_and_on_panic() {
        let d = TmpDir::new("swtest-guard");
        let root = d.to_path_buf();
        std::fs::write(d.join("x"), "x").unwrap();
        let f = TmpDir::new("swtest-guard").child("f.json");
        assert_ne!(f.parent().unwrap(), root.as_path());
        let froot = f.parent().unwrap().to_path_buf();
        drop(d);
        drop(f);
        assert!(!root.exists() && !froot.exists());
        let d = TmpDir::new("swtest-guard");
        let root = d.to_path_buf();
        assert!(
            std::panic::catch_unwind(move || {
                let _d = d;
                panic!("an expected panic: the guard still removes the directory");
            })
            .is_err()
        );
        assert!(!root.exists());
    }
}
