//! Segments, the box's own copy, and their delivery state (telemetry.py 558-698): `segment_bytes`,
//! `read_segment`, `Segments` (state.json `{name: {"todo": [row indices not yet in D1], "r2": uploaded?, "rows":
//! n}}`, retention by age and total size, oldest first).
//!
//! The gzip stream is miniz_oxide's (Python: zlib) with Python's header (`mtime=0`, no name, XFL 2, OS 255); the
//! decompressed NDJSON is byte-identical (as ship's archive, PORTING.md "Divergences").

use super::json::{J, OMap, loads};
use super::{PyExc, TResult};
use flate2::{Compression, GzBuilder, read::MultiGzDecoder};
use regex::Regex;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::sync::{LazyLock, Mutex};
use std::time::UNIX_EPOCH;

/// telemetry.py `SEGMENT_NAME.match(name)` (`\d`: any `Nd`; `$`: also before one final "\n").
pub static SEGMENT_NAME: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^(\d{8}T\d{4})-([0-9a-f]{8})-(\d+)-(\d+)\.ndjson\.gz\n?\z").expect("SEGMENT_NAME")
});

/// `segment_bytes(rows)`: gzip of one compact JSON line per row.
///
/// # Errors
/// An `OSError` if compressing into memory fails (it can't).
pub fn segment_bytes(rows: &[J]) -> TResult<Vec<u8>> {
    let mut text = String::new();
    for r in rows {
        text.push_str(&r.dumps_compact());
        text.push('\n');
    }
    let mut gz = GzBuilder::new()
        .mtime(0)
        .operating_system(255)
        .write(Vec::new(), Compression::best());
    gz.write_all(text.as_bytes())
        .and_then(|()| gz.finish())
        .map_err(|e| PyExc::from_io(&e, None))
}

/// `read_segment(path)`: the rows, or None where Python raises `OSError`/`ValueError`/`EOFError` (unreadable,
/// not gzip, truncated, not UTF-8, a line that isn't JSON). A corrupt deflate stream is None too (Python: an
/// uncaught `zlib.error`; PORTING.md "Divergences").
pub fn read_segment(path: &Path) -> Option<Vec<J>> {
    let f = std::fs::File::open(path).ok()?;
    let mut text = String::new();
    MultiGzDecoder::new(f).read_to_string(&mut text).ok()?;
    // text mode: universal newlines ("\r", "\r\n" end lines too)
    let text = text.replace("\r\n", "\n").replace('\r', "\n");
    let mut rows = Vec::new();
    for line in text.split_inclusive('\n') {
        if smallworld_core::pystr::strip_ws(line).is_empty() {
            continue;
        }
        rows.push(loads(line)?);
    }
    Some(rows)
}

/// The `*.ndjson.gz` entries of a directory (pathlib's glob: dot files too), as (name, path).
fn glob(dir: &Path) -> Vec<(String, PathBuf)> {
    let Ok(rd) = std::fs::read_dir(dir) else {
        return Vec::new();
    };
    rd.filter_map(Result::ok)
        .filter_map(|e| {
            let name = e.file_name().into_string().ok()?;
            name.ends_with(".ndjson.gz").then(|| (name, e.path()))
        })
        .collect()
}

fn todo_of(st: &J) -> Vec<J> {
    st.as_obj()
        .and_then(|m| m.get("todo"))
        .and_then(J::as_list)
        .cloned()
        .unwrap_or_default()
}

/// telemetry.py `Segments`.
#[derive(Debug)]
pub struct Segments {
    pub directory: PathBuf,
    pub days: i64,
    pub cap_mb: i64,
    pub clock: fn() -> f64,
    lock: Mutex<()>,
}

impl Segments {
    pub fn new(directory: impl Into<PathBuf>, days: i64, cap_mb: i64, clock: fn() -> f64) -> Self {
        Self {
            directory: directory.into(),
            days,
            cap_mb,
            clock,
            lock: Mutex::new(()),
        }
    }

    pub fn state_file(&self) -> PathBuf {
        self.directory.join("state.json")
    }

    /// `load()`: the entries whose name is a segment's and whose value is an object, in the file's order.
    pub fn load(&self) -> OMap {
        let Ok(text) = std::fs::read(self.state_file()) else {
            return OMap::new();
        };
        // read_text(): UTF-8 (a BOM is kept, so json.loads refuses it)
        let Some(J::Obj(data)) = std::str::from_utf8(&text).ok().and_then(loads) else {
            return OMap::new();
        };
        OMap(
            data.0
                .into_iter()
                .filter(|(k, v)| SEGMENT_NAME.is_match(k) && v.as_obj().is_some())
                .collect(),
        )
    }

    /// `save(state)`: `json.dumps(state, sort_keys=True)` to state.json.tmp, then renamed.
    ///
    /// # Errors
    /// The `OSError`.
    pub fn save(&self, state: &OMap) -> std::io::Result<()> {
        let tmp = self.directory.join("state.json.tmp");
        let text = smallworld_core::pyjson::dumps_sorted(&J::Obj(state.clone()).to_value());
        std::fs::write(&tmp, text)?;
        std::fs::rename(&tmp, self.state_file())
    }

    /// `write(rows, acked, src)`: one segment from the buffered rows, in one write; its name.
    ///
    /// # Errors
    /// The `OSError` (mkdir, write, rename, state.json, prune's stats).
    pub fn write(&self, rows: &[J], acked: &[String], src: &str) -> TResult<Option<String>> {
        let (Some(first), Some(last)) = (rows.first(), rows.last()) else {
            return Ok(None);
        };
        let field = |r: &J, k: &str| {
            r.as_obj()
                .and_then(|m| m.get(k))
                .cloned()
                .unwrap_or(J::Null)
        };
        #[allow(clippy::cast_precision_loss)]
        let ts = match field(first, "ts") {
            J::Int(n) => n as f64 / 1000.0,
            J::Float(x) => x / 1000.0,
            _ => 0.0,
        };
        let tm = crate::sys::gmtime(ts);
        let start = format!(
            "{:04}{:02}{:02}T{:02}{:02}",
            tm.year, tm.month, tm.day, tm.hour, tm.minute
        );
        let name = format!(
            "{start}-{src}-{}-{}.ndjson.gz",
            field(first, "seq").py_str(),
            field(last, "seq").py_str()
        );
        let data = segment_bytes(rows)?;
        let _g = crate::daemon::lock(&self.lock);
        std::fs::create_dir_all(&self.directory)
            .map_err(|e| PyExc::from_io(&e, Some(&self.directory)))?;
        let tmp = self.directory.join(format!(".{name}.tmp"));
        let dst = self.directory.join(&name);
        std::fs::write(&tmp, &data).map_err(|e| PyExc::from_io(&e, Some(&tmp)))?;
        std::fs::rename(&tmp, &dst).map_err(|e| PyExc::from_io2(&e, &tmp, &dst))?;
        let mut state = self.load();
        let todo: Vec<J> = rows
            .iter()
            .enumerate()
            .filter(|(_, r)| {
                let id = field(r, "id");
                !id.as_str().is_some_and(|id| acked.iter().any(|a| a == id))
            })
            .map(|(i, _)| J::from(i))
            .collect();
        state.insert(
            name.as_str(),
            OMap::new()
                .with("todo", J::List(todo))
                .with("r2", false)
                .with("rows", rows.len()),
        );
        self.save(&state).map_err(|e| PyExc::from_io(&e, None))?;
        self.prune_state(&mut state)
            .map_err(|e| PyExc::from_io(&e, None))?;
        Ok(Some(name))
    }

    /// `mark(name, rows=..., r2=...)`.
    ///
    /// # Errors
    /// The `OSError` of saving.
    pub fn mark(&self, name: &str, rows: &[J], r2: Option<bool>) -> std::io::Result<()> {
        let _g = crate::daemon::lock(&self.lock);
        let mut state = self.load();
        let Some(J::Obj(entry)) = state.get_mut(name) else {
            return Ok(());
        };
        let done = |i: &J| rows.iter().any(|d| py_eq(d, i));
        let todo: Vec<J> = todo_of(&J::Obj(entry.clone()))
            .into_iter()
            .filter(|i| !done(i))
            .collect();
        entry.insert("todo", J::List(todo));
        if let Some(r2) = r2 {
            entry.insert("r2", r2);
        }
        self.save(&state)
    }

    /// `prune()` (state loaded).
    ///
    /// # Errors
    /// The `OSError` of a stat or of saving.
    pub fn prune(&self) -> std::io::Result<Vec<String>> {
        let mut state = self.load();
        self.prune_state(&mut state)
    }

    /// `prune(state)`.
    ///
    /// # Errors
    /// The `OSError` of a stat or of saving.
    pub fn prune_state(&self, state: &mut OMap) -> std::io::Result<Vec<String>> {
        let mut files: Vec<(String, PathBuf)> = glob(&self.directory)
            .into_iter()
            .filter(|(n, _)| SEGMENT_NAME.is_match(n))
            .collect();
        files.sort();
        #[allow(clippy::cast_precision_loss)]
        let cutoff = (self.clock)() - (self.days * 86400) as f64;
        let mut total: u64 = 0;
        for (_, p) in &files {
            total += std::fs::metadata(p)?.len();
        }
        let cap = u64::try_from(self.cap_mb).unwrap_or(0) * 1_048_576;
        let mut dropped = Vec::new();
        for (name, p) in &files {
            let meta = std::fs::metadata(p)?;
            let mtime = meta
                .modified()?
                .duration_since(UNIX_EPOCH)
                .map_or(0.0, |d| d.as_secs_f64());
            let too_old = mtime < cutoff;
            if !too_old && total <= cap {
                break;
            }
            total = total.saturating_sub(std::fs::metadata(p)?.len());
            smallworld_core::fsx::unlink_missing_ok(p)?;
            dropped.push(name.clone());
            state.remove(name);
        }
        let gone: Vec<String> = state
            .keys()
            .filter(|n| !smallworld_core::fsx::exists(&self.directory.join(n.as_str())))
            .cloned()
            .collect();
        let forgotten = !gone.is_empty();
        for n in gone {
            state.remove(&n);
        }
        if !dropped.is_empty() || forgotten {
            // v1.5: a forgotten entry is saved too
            self.save(state)?;
        }
        Ok(dropped)
    }

    /// `pending()`: undelivered segments, oldest first.
    pub fn pending(&self) -> Vec<(String, J)> {
        let state = {
            let _g = crate::daemon::lock(&self.lock);
            self.load()
        };
        let mut out: Vec<(String, J)> = state
            .0
            .into_iter()
            .filter(|(_, s)| {
                let m = s.as_obj();
                m.and_then(|m| m.get("todo")).is_some_and(J::truthy)
                    || !m.and_then(|m| m.get("r2")).is_some_and(J::truthy)
            })
            .collect();
        out.sort_by(|a, b| a.0.cmp(&b.0));
        out
    }

    /// `backlog()`: the minute row's `backlog` object.
    pub fn backlog(&self) -> OMap {
        let pend = self.pending();
        let rows: usize = pend.iter().map(|(_, s)| todo_of(s).len()).sum();
        let mut size: u64 = 0;
        for (n, _) in &pend {
            if let Ok(m) = std::fs::metadata(self.directory.join(n)) {
                size += m.len();
            }
        }
        let entries = glob(&self.directory);
        let (mut total, mut count) = (0_u64, entries.len());
        for (_, p) in &entries {
            if let Ok(m) = std::fs::metadata(p) {
                total += m.len();
            } else {
                (total, count) = (0, 0);
                break;
            }
        }
        let oldest = pend
            .first()
            .map(|(n, _)| smallworld_core::pystr::prefix_cp(n, 13).to_string());
        OMap::new()
            .with("segments", pend.len())
            .with("rows", rows)
            .with("bytes", J::Int(i64::try_from(size).unwrap_or(i64::MAX)))
            .with("oldest", oldest)
            .with("stored_segments", count)
            .with(
                "stored_bytes",
                J::Int(i64::try_from(total).unwrap_or(i64::MAX)),
            )
    }
}

/// Python's `==` between two JSON values as `in`/`not in` uses it (ints, floats and bools compare by value).
pub fn py_eq(a: &J, b: &J) -> bool {
    #[allow(clippy::cast_precision_loss)]
    let num = |v: &J| match v {
        J::Int(n) => Some(*n as f64),
        J::Bool(x) => Some(f64::from(u8::from(*x))),
        J::Float(x) => Some(*x),
        _ => None,
    };
    match (num(a), num(b)) {
        (Some(x), Some(y)) => x == y,
        _ => a == b,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmpdir(tag: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("sw-tel-seg-{tag}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        d
    }

    #[test]
    fn write_mark_prune_like_python() {
        let d = tmpdir("a");
        let s = Segments::new(&d, 30, 200, crate::sys::now);
        let row = |seq: i64, id: &str| -> J {
            OMap::new()
                .with("v", 1_i64)
                .with("id", id)
                .with("seq", seq)
                .with("ts", 1_793_000_000_000_i64)
                .into()
        };
        let rows = vec![row(0, "abcd1234:0"), row(1, "abcd1234:1")];
        let name = s
            .write(&rows, &["abcd1234:0".to_string()], "abcd1234")
            .unwrap()
            .unwrap();
        assert_eq!(name, "20261026T0733-abcd1234-0-1.ndjson.gz");
        assert_eq!(
            std::fs::read_to_string(d.join("state.json")).unwrap(),
            r#"{"20261026T0733-abcd1234-0-1.ndjson.gz": {"r2": false, "rows": 2, "todo": [1]}}"#
        );
        assert_eq!(read_segment(&d.join(&name)).unwrap(), rows);
        let bytes = std::fs::read(d.join(&name)).unwrap();
        assert_eq!(&bytes[..10], &[0x1f, 0x8b, 8, 0, 0, 0, 0, 0, 2, 255]);
        assert_eq!(s.pending().len(), 1);
        s.mark(&name, &[J::Int(1)], Some(true)).unwrap();
        assert!(s.pending().is_empty());
        assert_eq!(s.backlog().get("stored_segments"), Some(&J::Int(1)));
        let old = Segments::new(&d, 30, 200, || 1e12);
        assert_eq!(old.prune().unwrap(), vec![name]);
        assert_eq!(std::fs::read_to_string(d.join("state.json")).unwrap(), "{}");
        std::fs::remove_dir_all(&d).unwrap();
    }
}
