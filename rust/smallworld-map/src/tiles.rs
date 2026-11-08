//! Tile files (`<out>/<ns>/<dim>/s<scale>/<tx>.<tz>.swt`) and atomic writes. Format (docs/reference.md
//! "World map", little-endian):
//!
//! ```text
//! off size    field
//! 0   8       magic "SWMAPT\0\x01"
//! 8   1       scale (0..4)
//! 9   1       flags (bit 0: provisional statuses included; always 0 today)
//! 10  2       reserved, 0
//! 12  4       tx (i32) = floor(blockX / (512 << scale))
//! 16  4       tz (i32)
//! 20  4       highest chunk DataVersion drawn (i32)
//! 24  8       rendered_at (unix seconds, i64)
//! 32  262144  packed colour ids, 512 rows of 512 (north to south, west to east); 0 = no data
//! ```
//!
//! A scale-s tile covers 2^s × 2^s regions. Readers (mc-brand's PreRender) never see a partial file: every write
//! goes to a temporary file in the same directory and is renamed over the old one. An all-zero tile is not
//! written (and an existing one is removed).

use std::fs;
use std::io::{self, Write};
use std::path::{Path, PathBuf};

pub const MAGIC: &[u8; 8] = b"SWMAPT\x00\x01";
pub const HEADER: usize = 32;
pub const SIDE: usize = 512;
pub const PIXELS: usize = SIDE * SIDE;
pub const SIZE: usize = HEADER + PIXELS;

#[derive(Clone)]
pub struct Tile {
    pub scale: u8,
    pub tx: i32,
    pub tz: i32,
    pub data_version: i32,
    pub rendered_at: i64,
    pub pixels: Vec<u8>,
}

impl Tile {
    #[must_use]
    pub fn empty(scale: u8, tx: i32, tz: i32) -> Tile {
        Tile {
            scale,
            tx,
            tz,
            data_version: 0,
            rendered_at: 0,
            pixels: vec![0; PIXELS],
        }
    }

    #[must_use]
    pub fn encode(&self) -> Vec<u8> {
        let mut out = Vec::with_capacity(SIZE);
        out.extend_from_slice(MAGIC);
        out.push(self.scale);
        out.push(0);
        out.extend_from_slice(&[0, 0]);
        out.extend_from_slice(&self.tx.to_le_bytes());
        out.extend_from_slice(&self.tz.to_le_bytes());
        out.extend_from_slice(&self.data_version.to_le_bytes());
        out.extend_from_slice(&self.rendered_at.to_le_bytes());
        out.extend_from_slice(&self.pixels);
        out
    }

    /// Parses a tile file; `None` if it isn't one (wrong size, magic or scale).
    #[must_use]
    pub fn decode(data: &[u8]) -> Option<Tile> {
        if data.len() != SIZE
            || &data[..8] != MAGIC
            || data[8] > 4
            || data[10] != 0
            || data[11] != 0
        {
            return None;
        }
        let i32_at =
            |o: usize| i32::from_le_bytes([data[o], data[o + 1], data[o + 2], data[o + 3]]);
        let mut ts = [0u8; 8];
        ts.copy_from_slice(&data[24..32]);
        Some(Tile {
            scale: data[8],
            tx: i32_at(12),
            tz: i32_at(16),
            data_version: i32_at(20),
            rendered_at: i64::from_le_bytes(ts),
            pixels: data[HEADER..].to_vec(),
        })
    }

    /// Copies a `side × side` block of pixels into the tile at pixel offset `(ox, oz)`.
    pub fn patch(&mut self, ox: usize, oz: usize, side: usize, block: &[u8]) {
        for z in 0..side {
            let dst = (oz + z) * SIDE + ox;
            self.pixels[dst..dst + side].copy_from_slice(&block[z * side..(z + 1) * side]);
        }
    }
}

#[must_use]
pub fn tile_path(dim_out: &Path, scale: u32, tx: i32, tz: i32) -> PathBuf {
    dim_out
        .join(format!("s{scale}"))
        .join(format!("{tx}.{tz}.swt"))
}

/// Writes `data` to `path` atomically: a temporary file next to it, then `rename`.
///
/// # Errors
/// Any I/O error (the temporary file is removed).
pub fn atomic_write(path: &Path, data: &[u8]) -> io::Result<()> {
    let dir = path
        .parent()
        .ok_or_else(|| io::Error::other("no parent directory"))?;
    fs::create_dir_all(dir)?;
    let name = path.file_name().and_then(|n| n.to_str()).unwrap_or("file");
    let tmp = dir.join(format!(".{name}.tmp{}", std::process::id()));
    let result = (|| {
        let mut f = fs::File::create(&tmp)?;
        f.write_all(data)?;
        f.flush()?;
        drop(f);
        fs::rename(&tmp, path)
    })();
    if result.is_err() {
        let _ = fs::remove_file(&tmp);
    }
    result
}

/// Writes a tile, or removes it when it has no pixels.
///
/// # Errors
/// Any I/O error.
pub fn store(dim_out: &Path, tile: &Tile) -> io::Result<bool> {
    let path = tile_path(dim_out, u32::from(tile.scale), tile.tx, tile.tz);
    if tile.pixels.iter().all(|&p| p == 0) {
        match fs::remove_file(&path) {
            Ok(()) => {}
            Err(e) if e.kind() == io::ErrorKind::NotFound => {}
            Err(e) => return Err(e),
        }
        return Ok(false);
    }
    atomic_write(&path, &tile.encode())?;
    Ok(true)
}

/// Loads a tile, or an empty one if it is missing or unreadable.
#[must_use]
pub fn load(dim_out: &Path, scale: u32, tx: i32, tz: i32) -> Tile {
    fs::read(tile_path(dim_out, scale, tx, tz))
        .ok()
        .and_then(|d| Tile::decode(&d))
        .filter(|t| u32::from(t.scale) == scale && t.tx == tx && t.tz == tz)
        .unwrap_or_else(|| Tile::empty(scale as u8, tx, tz))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trip() {
        let mut t = Tile::empty(3, -2, 5);
        t.data_version = 5023;
        t.rendered_at = 1_800_000_000;
        t.patch(64, 128, 64, &[9u8; 64 * 64]);
        let enc = t.encode();
        assert_eq!(enc.len(), SIZE);
        assert_eq!(&enc[..8], MAGIC);
        let back = Tile::decode(&enc).unwrap();
        assert_eq!(
            (back.scale, back.tx, back.tz, back.data_version),
            (3, -2, 5, 5023)
        );
        assert_eq!(back.pixels[128 * SIDE + 64], 9);
        assert_eq!(back.pixels[128 * SIDE + 63], 0);
        assert!(Tile::decode(&enc[..100]).is_none());
    }
}
