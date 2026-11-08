//! Region files (`r.<x>.<z>.mca`): the 8 KiB header, sector bounds, external `.mcc` chunks and the compression
//! dispatch, read with positioned reads only (never `mmap`: the server truncates and extends these files while it
//! runs, which would SIGBUS a mapping).
//!
//! The server may rewrite a chunk while it is being read. After reading a payload the chunk's header entry is read
//! again; if it moved, the read is retried (at most [`RETRIES`] times) and otherwise reported as [`ChunkError::Torn`]
//! so the caller keeps the chunk's previous data and tries again on the next run. A sector that lies beyond the end
//! of the file is treated the same way (the file is being extended).

use crate::compress::{self, CompressError};
use std::fmt;
use std::fs::File;
use std::io;
use std::os::unix::fs::FileExt;
use std::path::{Path, PathBuf};

pub const SECTOR: u64 = 4096;
pub const HEADER: usize = 8192;
pub const RETRIES: usize = 3;

#[derive(Debug)]
pub enum ChunkError {
    Io(io::Error),
    /// Changed while being read, or points past the end of the file: retry on the next run.
    Torn,
    /// A compression this reader doesn't implement (127 = custom), counted and skipped.
    Unsupported(u8),
    /// Malformed payload framing.
    Corrupt(&'static str),
    Decompress(CompressError),
}

impl fmt::Display for ChunkError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ChunkError::Io(e) => write!(f, "read error: {e}"),
            ChunkError::Torn => write!(f, "chunk changed while being read"),
            ChunkError::Unsupported(v) => write!(f, "unsupported compression {v}"),
            ChunkError::Corrupt(why) => write!(f, "corrupt chunk: {why}"),
            ChunkError::Decompress(e) => write!(f, "{e}"),
        }
    }
}

impl std::error::Error for ChunkError {}

impl From<io::Error> for ChunkError {
    fn from(e: io::Error) -> Self {
        ChunkError::Io(e)
    }
}

/// The parsed name of a region file.
#[must_use]
pub fn parse_name(name: &str) -> Option<(i32, i32)> {
    let rest = name.strip_prefix("r.")?.strip_suffix(".mca")?;
    let (x, z) = rest.split_once('.')?;
    Some((x.parse().ok()?, z.parse().ok()?))
}

/// One open region file.
pub struct Region {
    file: File,
    dir: PathBuf,
    pub rx: i32,
    pub rz: i32,
    header: Vec<u8>,
}

/// A chunk's decompressed NBT with the header timestamp that belongs to it.
pub struct RawChunk {
    pub nbt: Vec<u8>,
    pub timestamp: u32,
    pub compression: u8,
}

fn be32(b: &[u8], at: usize) -> u32 {
    u32::from_be_bytes([b[at], b[at + 1], b[at + 2], b[at + 3]])
}

impl Region {
    /// Opens a region file and reads its header. A file shorter than the header has no chunks (the server creates
    /// region files empty and writes the header with the first chunk).
    ///
    /// # Errors
    /// The file can't be opened or read.
    pub fn open(path: &Path, rx: i32, rz: i32) -> io::Result<Region> {
        let file = File::open(path)?;
        let mut header = vec![0u8; HEADER];
        let len = file.metadata()?.len();
        if len >= HEADER as u64 {
            file.read_exact_at(&mut header, 0)?;
        }
        let dir = path.parent().map_or_else(PathBuf::new, Path::to_path_buf);
        Ok(Region {
            file,
            dir,
            rx,
            rz,
            header,
        })
    }

    /// `(sector offset, sector count)` of chunk `idx` (0 = absent).
    #[must_use]
    pub fn location(&self, idx: usize) -> (u32, u32) {
        let v = be32(&self.header, idx * 4);
        (v >> 8, v & 0xff)
    }

    #[must_use]
    pub fn timestamp(&self, idx: usize) -> u32 {
        be32(&self.header, 4096 + idx * 4)
    }

    /// All 1024 header timestamps.
    #[must_use]
    pub fn timestamps(&self) -> Vec<u32> {
        (0..1024).map(|i| self.timestamp(i)).collect()
    }

    fn reread_entry(&mut self, idx: usize) -> io::Result<bool> {
        let mut loc = [0u8; 4];
        let mut ts = [0u8; 4];
        if self.file.metadata()?.len() < HEADER as u64 {
            return Ok(true);
        }
        self.file.read_exact_at(&mut loc, (idx * 4) as u64)?;
        self.file.read_exact_at(&mut ts, (4096 + idx * 4) as u64)?;
        let changed = self.header[idx * 4..idx * 4 + 4] != loc
            || self.header[4096 + idx * 4..4100 + idx * 4] != ts;
        self.header[idx * 4..idx * 4 + 4].copy_from_slice(&loc);
        self.header[4096 + idx * 4..4100 + idx * 4].copy_from_slice(&ts);
        Ok(changed)
    }

    /// Reads and decompresses chunk `idx` (`(x & 31) + (z & 31) * 32`). `Ok(None)`: the chunk is absent.
    ///
    /// # Errors
    /// See [`ChunkError`]; every error leaves the region usable for the next chunk.
    pub fn read_chunk(&mut self, idx: usize) -> Result<Option<RawChunk>, ChunkError> {
        for _ in 0..=RETRIES {
            match self.try_read(idx) {
                Err(ChunkError::Torn) => {
                    if !self.reread_entry(idx)? {
                        // Header unchanged but the data is bad or out of bounds: it's not a race.
                        return Err(ChunkError::Corrupt("sector outside the file"));
                    }
                }
                Ok(Some((payload, ts))) => {
                    if self.reread_entry(idx)? {
                        continue; // rewritten while we read it
                    }
                    let compression = payload.0;
                    return decode(compression, &payload.1, &self.dir, self.rx, self.rz, idx).map(
                        |nbt| {
                            Some(RawChunk {
                                nbt,
                                timestamp: ts,
                                compression: compression & 0x7f,
                            })
                        },
                    );
                }
                Ok(None) => return Ok(None),
                Err(e) => return Err(e),
            }
        }
        Err(ChunkError::Torn)
    }

    /// One attempt: the payload's version byte and bytes, or `Torn` when it lies past the end of the file.
    #[allow(clippy::type_complexity)]
    fn try_read(&self, idx: usize) -> Result<Option<((u8, Vec<u8>), u32)>, ChunkError> {
        let (off, count) = self.location(idx);
        if off == 0 && count == 0 {
            return Ok(None);
        }
        if off < 2 || count == 0 {
            return Err(ChunkError::Corrupt("sector inside the header"));
        }
        let ts = self.timestamp(idx);
        let start = u64::from(off) * SECTOR;
        let size = u64::from(count) * SECTOR;
        let len = self.file.metadata()?.len();
        if start + 5 > len {
            return Err(ChunkError::Torn);
        }
        let mut head = [0u8; 5];
        self.file.read_exact_at(&mut head, start)?;
        let length = u64::from(be32(&head, 0));
        if length == 0 {
            return Err(ChunkError::Corrupt("zero-length chunk"));
        }
        if length + 4 > size {
            return Err(ChunkError::Corrupt("chunk longer than its sectors"));
        }
        if start + 4 + length > len {
            return Err(ChunkError::Torn);
        }
        let mut data = vec![0u8; (length - 1) as usize];
        match self.file.read_exact_at(&mut data, start + 5) {
            Ok(()) => {}
            Err(e) if e.kind() == io::ErrorKind::UnexpectedEof => return Err(ChunkError::Torn),
            Err(e) => return Err(e.into()),
        }
        Ok(Some(((head[4], data), ts)))
    }
}

/// Decompresses a payload by its version byte; bit 0x80 means the stream is in `c.<x>.<z>.mcc` next to the region
/// (absolute chunk coordinates), written whole by the server (temp file + move).
fn decode(
    version: u8,
    data: &[u8],
    dir: &Path,
    rx: i32,
    rz: i32,
    idx: usize,
) -> Result<Vec<u8>, ChunkError> {
    let compression = version & 0x7f;
    if version & 0x80 != 0 {
        // Vanilla reads the external stream even when an internal one is also present (it only logs it).
        let cx = rx * 32 + (idx & 31) as i32;
        let cz = rz * 32 + (idx >> 5) as i32;
        let ext = std::fs::read(dir.join(format!("c.{cx}.{cz}.mcc"))).map_err(|e| {
            if e.kind() == io::ErrorKind::NotFound {
                ChunkError::Torn
            } else {
                ChunkError::Io(e)
            }
        })?;
        return decompress(compression, &ext);
    }
    decompress(compression, data)
}

/// Decompresses one chunk stream by its compression id.
///
/// # Errors
/// Unsupported ids (127 custom) and malformed streams.
pub fn decompress(compression: u8, data: &[u8]) -> Result<Vec<u8>, ChunkError> {
    match compression {
        1 => compress::gzip(data).map_err(ChunkError::Decompress),
        2 => compress::zlib(data).map_err(ChunkError::Decompress),
        3 => {
            if data.len() > compress::MAX_OUTPUT {
                return Err(ChunkError::Decompress(CompressError::TooLarge));
            }
            Ok(data.to_vec())
        }
        4 => compress::lz4_stream(data).map_err(ChunkError::Decompress),
        v => Err(ChunkError::Unsupported(v)),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn names() {
        assert_eq!(parse_name("r.-1.20.mca"), Some((-1, 20)));
        assert_eq!(parse_name("r.1.mca"), None);
        assert_eq!(parse_name("r.a.b.mca"), None);
        assert_eq!(parse_name("c.1.2.mcc"), None);
    }
}
