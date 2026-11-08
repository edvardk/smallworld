//! Incremental state under `<out>/.work/<ns>/<dim>/`:
//!
//! - `dimension.state` (text): the fingerprint (palette, renderer version, radius), the world folder's identity
//!   (`st_dev`, `st_ino`) and the output generation. A different fingerprint or world means a full re-render.
//! - `r.<x>.<z>.state` (binary, per region): the region file's mtime and size, its 1024 chunk timestamps, whether
//!   every chunk was read (a torn chunk leaves the region incomplete, so the next run retries it), the highest
//!   DataVersion drawn, and per scale the edge rows the neighbours need (top-row aggregates, bottom-row heights).
//!
//! A region is re-rendered when its mtime or size changed *and* a chunk timestamp changed, or when it is incomplete.

use crate::scale::{Cell, REGION, SCALES};
use crate::tiles::atomic_write;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};

const MAGIC: &[u8; 8] = b"SWMAPR\x00\x01";

#[derive(Clone, Default)]
pub struct Edges {
    pub top: Vec<Cell>,
    pub bottom: Vec<Option<f64>>,
}

#[derive(Clone)]
pub struct RegionState {
    pub fingerprint: u64,
    pub mtime_ns: i64,
    pub size: u64,
    pub complete: bool,
    pub data_version: i32,
    pub timestamps: Vec<u32>,
    /// Per scale 0..=4.
    pub edges: Vec<Edges>,
}

#[must_use]
pub fn region_state_path(work: &Path, rx: i32, rz: i32) -> PathBuf {
    work.join(format!("r.{rx}.{rz}.state"))
}

struct Cursor<'a> {
    d: &'a [u8],
    p: usize,
}

impl Cursor<'_> {
    fn take<const N: usize>(&mut self) -> Option<[u8; N]> {
        let s = self.d.get(self.p..self.p + N)?;
        self.p += N;
        let mut a = [0u8; N];
        a.copy_from_slice(s);
        Some(a)
    }
    fn u8(&mut self) -> Option<u8> {
        Some(self.take::<1>()?[0])
    }
    fn u32(&mut self) -> Option<u32> {
        Some(u32::from_le_bytes(self.take()?))
    }
    fn u64(&mut self) -> Option<u64> {
        Some(u64::from_le_bytes(self.take()?))
    }
}

impl RegionState {
    #[must_use]
    pub fn encode(&self) -> Vec<u8> {
        let mut o = Vec::with_capacity(32 * 1024);
        o.extend_from_slice(MAGIC);
        o.extend_from_slice(&self.fingerprint.to_le_bytes());
        o.extend_from_slice(&self.mtime_ns.to_le_bytes());
        o.extend_from_slice(&self.size.to_le_bytes());
        o.push(u8::from(self.complete));
        o.extend_from_slice(&self.data_version.to_le_bytes());
        for t in &self.timestamps {
            o.extend_from_slice(&t.to_le_bytes());
        }
        for e in &self.edges {
            for c in &e.top {
                match c {
                    Some((colour, d1, l2)) => {
                        o.push(1);
                        o.push(*colour);
                        o.extend_from_slice(&d1.to_bits().to_le_bytes());
                        o.extend_from_slice(&l2.to_le_bytes());
                    }
                    None => o.extend_from_slice(&[0u8; 18]),
                }
            }
            for b in &e.bottom {
                o.push(u8::from(b.is_some()));
                o.extend_from_slice(&b.unwrap_or(0.0).to_bits().to_le_bytes());
            }
        }
        o
    }

    #[must_use]
    pub fn decode(d: &[u8]) -> Option<RegionState> {
        let mut c = Cursor { d, p: 0 };
        if &c.take::<8>()? != MAGIC {
            return None;
        }
        let fingerprint = c.u64()?;
        let mtime_ns = c.u64()? as i64;
        let size = c.u64()?;
        let complete = c.u8()? == 1;
        let data_version = c.u32()? as i32;
        let timestamps = (0..1024).map(|_| c.u32()).collect::<Option<Vec<_>>>()?;
        let mut edges = Vec::new();
        for s in 0..SCALES {
            let n = REGION >> s;
            let mut top = Vec::with_capacity(n);
            for _ in 0..n {
                let valid = c.u8()?;
                let colour = c.u8()?;
                let d1 = f64::from_bits(c.u64()?);
                let l2 = c.u64()? as i64;
                top.push((valid == 1).then_some((colour, d1, l2)));
            }
            let mut bottom = Vec::with_capacity(n);
            for _ in 0..n {
                let valid = c.u8()?;
                let d1 = f64::from_bits(c.u64()?);
                bottom.push((valid == 1).then_some(d1));
            }
            edges.push(Edges { top, bottom });
        }
        (c.p == d.len()).then_some(RegionState {
            fingerprint,
            mtime_ns,
            size,
            complete,
            data_version,
            timestamps,
            edges,
        })
    }

    /// The region's state, if present, readable and made with this fingerprint.
    #[must_use]
    pub fn load(work: &Path, rx: i32, rz: i32, fingerprint: u64) -> Option<RegionState> {
        let d = fs::read(region_state_path(work, rx, rz)).ok()?;
        RegionState::decode(&d).filter(|s| s.fingerprint == fingerprint)
    }

    /// # Errors
    /// Any I/O error.
    pub fn save(&self, work: &Path, rx: i32, rz: i32) -> io::Result<()> {
        atomic_write(&region_state_path(work, rx, rz), &self.encode())
    }
}

/// The dimension's state file.
#[derive(Clone, Debug, PartialEq, Eq, Default)]
pub struct DimState {
    pub fingerprint: u64,
    pub dev: u64,
    pub ino: u64,
    pub generation: u64,
}

impl DimState {
    #[must_use]
    pub fn load(work: &Path) -> Option<DimState> {
        let text = fs::read_to_string(work.join("dimension.state")).ok()?;
        let mut s = DimState::default();
        let mut format = false;
        for line in text.lines() {
            let (k, v) = line.split_once(' ')?;
            match k {
                "format" => format = v == "1",
                "fingerprint" => s.fingerprint = u64::from_str_radix(v, 16).ok()?,
                "dev" => s.dev = v.parse().ok()?,
                "ino" => s.ino = v.parse().ok()?,
                "generation" => s.generation = v.parse().ok()?,
                _ => {}
            }
        }
        format.then_some(s)
    }

    /// # Errors
    /// Any I/O error.
    pub fn save(&self, work: &Path) -> io::Result<()> {
        let text = format!(
            "format 1\nfingerprint {:016x}\ndev {}\nino {}\ngeneration {}\n",
            self.fingerprint, self.dev, self.ino, self.generation
        );
        atomic_write(&work.join("dimension.state"), text.as_bytes())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn region_state_round_trip() {
        let edges = (0..SCALES)
            .map(|s| {
                let n = REGION >> s;
                Edges {
                    top: (0..n)
                        .map(|i| (i % 3 != 0).then_some((7, i as f64 / 4.0, 2)))
                        .collect(),
                    bottom: vec![Some(-3.5); n],
                }
            })
            .collect();
        let st = RegionState {
            fingerprint: 9,
            mtime_ns: -5,
            size: 8192,
            complete: true,
            data_version: 5023,
            timestamps: vec![3; 1024],
            edges,
        };
        let back = RegionState::decode(&st.encode()).unwrap();
        assert_eq!(back.mtime_ns, -5);
        assert_eq!(back.edges[2].top[4], Some((7, 1.0, 2)));
        assert_eq!(back.edges[2].top[3], None);
        assert_eq!(back.edges[4].bottom[31], Some(-3.5));
        assert!(RegionState::decode(&st.encode()[..100]).is_none());
    }
}
