//! Decompression for region-file chunk payloads, std only: a DEFLATE decoder (RFC 1951) behind zlib (RFC 1950,
//! region compression 2, the default) and gzip (RFC 1952, compression 1) wrappers, and lz4-java's `LZ4Block` stream
//! (compression 4).
//!
//! Every stream carries a checksum and every one is verified (zlib's Adler-32, gzip's CRC-32 and length,
//! LZ4Block's masked XXH32 per block), so a decoder bug or a torn read shows up as an error, never as a wrong map.
//! The output is capped ([`MAX_OUTPUT`]) so a crafted payload can't exhaust memory.

use std::fmt;

/// The largest decompressed chunk accepted (vanilla's own chunks are well under 1 MiB).
pub const MAX_OUTPUT: usize = 64 << 20;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CompressError {
    Truncated,
    Corrupt(&'static str),
    Checksum,
    TooLarge,
}

impl fmt::Display for CompressError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            CompressError::Truncated => write!(f, "compressed stream truncated"),
            CompressError::Corrupt(why) => write!(f, "corrupt compressed stream: {why}"),
            CompressError::Checksum => write!(f, "checksum mismatch"),
            CompressError::TooLarge => write!(f, "decompressed size above {MAX_OUTPUT} bytes"),
        }
    }
}

impl std::error::Error for CompressError {}

type Result<T> = std::result::Result<T, CompressError>;

// ---------------------------------------------------------------------------------------------------------------
// DEFLATE

struct Bits<'a> {
    data: &'a [u8],
    pos: usize,
    buf: u64,
    cnt: u32,
}

impl Bits<'_> {
    #[inline]
    fn refill(&mut self) {
        while self.cnt <= 56 {
            let Some(&b) = self.data.get(self.pos) else {
                break;
            };
            self.buf |= u64::from(b) << self.cnt;
            self.pos += 1;
            self.cnt += 8;
        }
    }

    #[inline]
    fn need(&mut self, n: u32) -> Result<()> {
        if self.cnt < n {
            self.refill();
            if self.cnt < n {
                return Err(CompressError::Truncated);
            }
        }
        Ok(())
    }

    #[inline]
    fn get(&mut self, n: u32) -> Result<u32> {
        if n == 0 {
            return Ok(0);
        }
        self.need(n)?;
        let v = (self.buf & ((1u64 << n) - 1)) as u32;
        self.buf >>= n;
        self.cnt -= n;
        Ok(v)
    }

    /// Drops the bits up to the next byte boundary.
    fn align(&mut self) {
        let drop = self.cnt % 8;
        self.buf >>= drop;
        self.cnt -= drop;
    }

    /// The byte offset just past what has been consumed (after [`Bits::align`]).
    fn consumed(&self) -> usize {
        self.pos - (self.cnt / 8) as usize
    }
}

const FAST_BITS: u32 = 10;

/// A canonical Huffman code: a direct lookup for codes up to [`FAST_BITS`] long and the counts/symbols for the
/// rest (decoded one bit at a time, as zlib's puff.c does).
struct Huffman {
    /// `symbol << 4 | length`, 0 = longer than FAST_BITS (or unused).
    fast: Vec<u16>,
    counts: [u16; 16],
    symbols: Vec<u16>,
}

impl Huffman {
    fn new(lengths: &[u8]) -> Result<Huffman> {
        let mut counts = [0u16; 16];
        for &l in lengths {
            counts[usize::from(l)] += 1;
        }
        counts[0] = 0;
        let mut left: i32 = 1;
        for &c in &counts[1..] {
            left = (left << 1) - i32::from(c);
            if left < 0 {
                return Err(CompressError::Corrupt("over-subscribed Huffman code"));
            }
        }
        let mut offs = [0u16; 16];
        for len in 1..15 {
            offs[len + 1] = offs[len] + counts[len];
        }
        let mut symbols = vec![0u16; lengths.len()];
        for (sym, &l) in lengths.iter().enumerate() {
            if l != 0 {
                symbols[usize::from(offs[usize::from(l)])] = sym as u16;
                offs[usize::from(l)] += 1;
            }
        }
        // Fast table: walk the canonical codes in order.
        let mut fast = vec![0u16; 1 << FAST_BITS];
        let mut code: u32 = 0;
        let mut idx = 0usize;
        for len in 1..=15u32 {
            for _ in 0..counts[len as usize] {
                if len <= FAST_BITS {
                    let rev = code.reverse_bits() >> (32 - len);
                    let entry = (symbols[idx] << 4) | len as u16;
                    let mut i = rev;
                    while i < (1 << FAST_BITS) {
                        fast[i as usize] = entry;
                        i += 1 << len;
                    }
                }
                code += 1;
                idx += 1;
            }
            code <<= 1;
        }
        Ok(Huffman {
            fast,
            counts,
            symbols,
        })
    }

    #[inline]
    fn decode(&self, bits: &mut Bits<'_>) -> Result<u16> {
        if bits.cnt < 15 {
            bits.refill();
        }
        let e = self.fast[(bits.buf & ((1 << FAST_BITS) - 1)) as usize];
        if e != 0 {
            let len = u32::from(e & 15);
            if len <= bits.cnt {
                bits.buf >>= len;
                bits.cnt -= len;
                return Ok(e >> 4);
            }
        }
        // Slow path: canonical decoding bit by bit.
        let mut code: i32 = 0;
        let mut first: i32 = 0;
        let mut index: i32 = 0;
        for len in 1..16 {
            code |= bits.get(1)? as i32;
            let count = i32::from(self.counts[len]);
            if code - first < count {
                return Ok(self.symbols[(index + code - first) as usize]);
            }
            index += count;
            first = (first + count) << 1;
            code <<= 1;
        }
        Err(CompressError::Corrupt("invalid Huffman code"))
    }
}

const LEN_BASE: [u16; 29] = [
    3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 15, 17, 19, 23, 27, 31, 35, 43, 51, 59, 67, 83, 99, 115, 131,
    163, 195, 227, 258,
];
const LEN_EXTRA: [u8; 29] = [
    0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3, 4, 4, 4, 4, 5, 5, 5, 5, 0,
];
const DIST_BASE: [u16; 30] = [
    1, 2, 3, 4, 5, 7, 9, 13, 17, 25, 33, 49, 65, 97, 129, 193, 257, 385, 513, 769, 1025, 1537,
    2049, 3073, 4097, 6145, 8193, 12289, 16385, 24577,
];
const DIST_EXTRA: [u8; 30] = [
    0, 0, 0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6, 7, 7, 8, 8, 9, 9, 10, 10, 11, 11, 12, 12, 13,
    13,
];
const CL_ORDER: [usize; 19] = [
    16, 17, 18, 0, 8, 7, 9, 6, 10, 5, 11, 4, 12, 3, 13, 2, 14, 1, 15,
];

fn codes(
    bits: &mut Bits<'_>,
    out: &mut Vec<u8>,
    lit: &Huffman,
    dist: &Huffman,
    limit: usize,
) -> Result<()> {
    loop {
        let sym = lit.decode(bits)?;
        if sym < 256 {
            if out.len() >= limit {
                return Err(CompressError::TooLarge);
            }
            out.push(sym as u8);
        } else if sym == 256 {
            return Ok(());
        } else {
            let s = usize::from(sym - 257);
            if s >= 29 {
                return Err(CompressError::Corrupt("bad length symbol"));
            }
            let len = usize::from(LEN_BASE[s]) + bits.get(u32::from(LEN_EXTRA[s]))? as usize;
            let d = usize::from(dist.decode(bits)?);
            if d >= 30 {
                return Err(CompressError::Corrupt("bad distance symbol"));
            }
            let distance = usize::from(DIST_BASE[d]) + bits.get(u32::from(DIST_EXTRA[d]))? as usize;
            if distance > out.len() {
                return Err(CompressError::Corrupt("distance too far back"));
            }
            if out.len() + len > limit {
                return Err(CompressError::TooLarge);
            }
            let start = out.len() - distance;
            if distance >= len {
                out.extend_from_within(start..start + len);
            } else {
                for i in 0..len {
                    let b = out[start + i];
                    out.push(b);
                }
            }
        }
    }
}

/// Inflates a raw DEFLATE stream. Returns the output and the number of input bytes used.
///
/// # Errors
/// A truncated or malformed stream, or more output than `limit`.
pub fn inflate(data: &[u8], limit: usize) -> Result<(Vec<u8>, usize)> {
    let mut bits = Bits {
        data,
        pos: 0,
        buf: 0,
        cnt: 0,
    };
    let mut out = Vec::with_capacity(data.len().saturating_mul(6).min(limit)); // chunks inflate ~7x
    loop {
        let last = bits.get(1)?;
        match bits.get(2)? {
            0 => {
                bits.align();
                let len = bits.get(16)? as usize;
                let nlen = bits.get(16)? as usize;
                if len != !nlen & 0xffff {
                    return Err(CompressError::Corrupt("stored block length"));
                }
                if out.len() + len > limit {
                    return Err(CompressError::TooLarge);
                }
                // Bytes still in the bit buffer come first, then straight from the input.
                let mut n = len;
                while n > 0 && bits.cnt >= 8 {
                    out.push(bits.get(8)? as u8);
                    n -= 1;
                }
                let end = bits.pos.checked_add(n).ok_or(CompressError::Truncated)?;
                let chunk = data.get(bits.pos..end).ok_or(CompressError::Truncated)?;
                out.extend_from_slice(chunk);
                bits.pos = end;
            }
            1 => {
                let mut l = [0u8; 288];
                l[..144].fill(8);
                l[144..256].fill(9);
                l[256..280].fill(7);
                l[280..].fill(8);
                let lit = Huffman::new(&l)?;
                let dist = Huffman::new(&[5u8; 30])?;
                codes(&mut bits, &mut out, &lit, &dist, limit)?;
            }
            2 => {
                let nlen = bits.get(5)? as usize + 257;
                let ndist = bits.get(5)? as usize + 1;
                let ncode = bits.get(4)? as usize + 4;
                if nlen > 286 || ndist > 30 {
                    return Err(CompressError::Corrupt("bad code counts"));
                }
                let mut cl = [0u8; 19];
                for &i in &CL_ORDER[..ncode] {
                    cl[i] = bits.get(3)? as u8;
                }
                let clh = Huffman::new(&cl)?;
                let mut lengths = vec![0u8; nlen + ndist];
                let mut i = 0;
                while i < nlen + ndist {
                    let sym = clh.decode(&mut bits)?;
                    let (val, rep) = match sym {
                        0..=15 => (sym as u8, 1),
                        16 => {
                            if i == 0 {
                                return Err(CompressError::Corrupt(
                                    "repeat with no previous length",
                                ));
                            }
                            (lengths[i - 1], 3 + bits.get(2)? as usize)
                        }
                        17 => (0, 3 + bits.get(3)? as usize),
                        _ => (0, 11 + bits.get(7)? as usize),
                    };
                    if i + rep > nlen + ndist {
                        return Err(CompressError::Corrupt("code lengths overflow"));
                    }
                    lengths[i..i + rep].fill(val);
                    i += rep;
                }
                if lengths[256] == 0 {
                    return Err(CompressError::Corrupt("no end-of-block code"));
                }
                let lit = Huffman::new(&lengths[..nlen])?;
                let dist = Huffman::new(&lengths[nlen..])?;
                codes(&mut bits, &mut out, &lit, &dist, limit)?;
            }
            _ => return Err(CompressError::Corrupt("reserved block type")),
        }
        if last == 1 {
            break;
        }
    }
    bits.align();
    Ok((out, bits.consumed()))
}

// ---------------------------------------------------------------------------------------------------------------
// Checksums

/// Adler-32 (zlib's trailer).
#[must_use]
pub fn adler32(data: &[u8]) -> u32 {
    let (mut a, mut b) = (1u32, 0u32);
    for chunk in data.chunks(5552) {
        for &x in chunk {
            a += u32::from(x);
            b += a;
        }
        a %= 65521;
        b %= 65521;
    }
    (b << 16) | a
}

const fn crc_table() -> [u32; 256] {
    let mut t = [0u32; 256];
    let mut i = 0;
    while i < 256 {
        let mut c = i as u32;
        let mut k = 0;
        while k < 8 {
            c = if c & 1 != 0 {
                0xEDB8_8320 ^ (c >> 1)
            } else {
                c >> 1
            };
            k += 1;
        }
        t[i] = c;
        i += 1;
    }
    t
}

static CRC_TABLE: [u32; 256] = crc_table();

/// CRC-32 (IEEE; gzip's trailer, PNG chunks).
#[must_use]
pub fn crc32(data: &[u8]) -> u32 {
    let mut c = !0u32;
    for &b in data {
        c = CRC_TABLE[((c ^ u32::from(b)) & 0xff) as usize] ^ (c >> 8);
    }
    !c
}

const P1: u32 = 0x9E37_79B1;
const P2: u32 = 0x85EB_CA77;
const P3: u32 = 0xC2B2_AE3D;
const P4: u32 = 0x27D4_EB2F;
const P5: u32 = 0x1656_67B1;

fn le32(b: &[u8]) -> u32 {
    u32::from_le_bytes([b[0], b[1], b[2], b[3]])
}

/// XXH32 (the hash lz4-java's block stream checks).
#[must_use]
pub fn xxh32(data: &[u8], seed: u32) -> u32 {
    let mut i = 0;
    let mut h: u32;
    if data.len() >= 16 {
        let mut v = [
            seed.wrapping_add(P1).wrapping_add(P2),
            seed.wrapping_add(P2),
            seed,
            seed.wrapping_sub(P1),
        ];
        while i + 16 <= data.len() {
            for (j, acc) in v.iter_mut().enumerate() {
                *acc = acc
                    .wrapping_add(le32(&data[i + j * 4..]).wrapping_mul(P2))
                    .rotate_left(13)
                    .wrapping_mul(P1);
            }
            i += 16;
        }
        h = v[0]
            .rotate_left(1)
            .wrapping_add(v[1].rotate_left(7))
            .wrapping_add(v[2].rotate_left(12))
            .wrapping_add(v[3].rotate_left(18));
    } else {
        h = seed.wrapping_add(P5);
    }
    h = h.wrapping_add(data.len() as u32);
    while i + 4 <= data.len() {
        h = h
            .wrapping_add(le32(&data[i..]).wrapping_mul(P3))
            .rotate_left(17)
            .wrapping_mul(P4);
        i += 4;
    }
    while i < data.len() {
        h = h
            .wrapping_add(u32::from(data[i]).wrapping_mul(P5))
            .rotate_left(11)
            .wrapping_mul(P1);
        i += 1;
    }
    h ^= h >> 15;
    h = h.wrapping_mul(P2);
    h ^= h >> 13;
    h = h.wrapping_mul(P3);
    h ^= h >> 16;
    h
}

// ---------------------------------------------------------------------------------------------------------------
// Containers

/// zlib (region compression 2): header, DEFLATE, Adler-32.
///
/// # Errors
/// A bad header, a malformed stream or a checksum mismatch.
pub fn zlib(data: &[u8]) -> Result<Vec<u8>> {
    if data.len() < 6 {
        return Err(CompressError::Truncated);
    }
    let (cmf, flg) = (data[0], data[1]);
    if cmf & 0x0f != 8 || (u16::from(cmf) << 8 | u16::from(flg)) % 31 != 0 || flg & 0x20 != 0 {
        return Err(CompressError::Corrupt("zlib header"));
    }
    let (out, used) = inflate(&data[2..], MAX_OUTPUT)?;
    let t = data
        .get(2 + used..2 + used + 4)
        .ok_or(CompressError::Truncated)?;
    if u32::from_be_bytes([t[0], t[1], t[2], t[3]]) != adler32(&out) {
        return Err(CompressError::Checksum);
    }
    Ok(out)
}

/// gzip (region compression 1): one member; header fields skipped, CRC-32 and length checked.
///
/// # Errors
/// A bad header, a malformed stream or a checksum mismatch.
pub fn gzip(data: &[u8]) -> Result<Vec<u8>> {
    if data.len() < 18 {
        return Err(CompressError::Truncated);
    }
    if data[0] != 0x1f || data[1] != 0x8b || data[2] != 8 {
        return Err(CompressError::Corrupt("gzip header"));
    }
    let flg = data[3];
    let mut p = 10usize;
    if flg & 4 != 0 {
        let x = data.get(p..p + 2).ok_or(CompressError::Truncated)?;
        p += 2 + usize::from(u16::from_le_bytes([x[0], x[1]]));
    }
    for bit in [8u8, 16] {
        if flg & bit != 0 {
            let rest = data.get(p..).ok_or(CompressError::Truncated)?;
            let z = rest
                .iter()
                .position(|&b| b == 0)
                .ok_or(CompressError::Truncated)?;
            p += z + 1;
        }
    }
    if flg & 2 != 0 {
        p += 2;
    }
    let body = data.get(p..).ok_or(CompressError::Truncated)?;
    let (out, used) = inflate(body, MAX_OUTPUT)?;
    let t = body.get(used..used + 8).ok_or(CompressError::Truncated)?;
    if le32(t) != crc32(&out) || le32(&t[4..]) != out.len() as u32 {
        return Err(CompressError::Checksum);
    }
    Ok(out)
}

/// lz4-java `LZ4BlockOutputStream`'s seed for the per-block XXH32 (`DEFAULT_SEED`).
pub const LZ4_SEED: u32 = 0x9747_B28C;

/// One LZ4 block (the raw block format) into `out`, which must end up exactly `size` bytes longer.
fn lz4_block(src: &[u8], out: &mut Vec<u8>, size: usize) -> Result<()> {
    let base = out.len();
    let end = base + size;
    let mut i = 0usize;
    let byte = |i: usize| src.get(i).copied().ok_or(CompressError::Truncated);
    loop {
        let token = byte(i)?;
        i += 1;
        let mut lit = usize::from(token >> 4);
        if lit == 15 {
            loop {
                let b = byte(i)?;
                i += 1;
                lit += usize::from(b);
                if b != 255 {
                    break;
                }
            }
        }
        let l = src
            .get(i..i.checked_add(lit).ok_or(CompressError::Truncated)?)
            .ok_or(CompressError::Truncated)?;
        if out.len() + lit > end {
            return Err(CompressError::Corrupt("LZ4 literals overflow the block"));
        }
        out.extend_from_slice(l);
        i += lit;
        if i == src.len() {
            break;
        }
        let off = usize::from(byte(i)?) | usize::from(byte(i + 1)?) << 8;
        i += 2;
        let mut mlen = usize::from(token & 15);
        if mlen == 15 {
            loop {
                let b = byte(i)?;
                i += 1;
                mlen += usize::from(b);
                if b != 255 {
                    break;
                }
            }
        }
        mlen += 4;
        if off == 0 || off > out.len() - base {
            return Err(CompressError::Corrupt("LZ4 offset"));
        }
        if out.len() + mlen > end {
            return Err(CompressError::Corrupt("LZ4 match overflows the block"));
        }
        let start = out.len() - off;
        for k in 0..mlen {
            let b = out[start + k];
            out.push(b);
        }
    }
    if out.len() != end {
        return Err(CompressError::Corrupt("LZ4 block size"));
    }
    Ok(())
}

/// lz4-java's `LZ4BlockInputStream` format (region compression 4): blocks of magic `LZ4Block`, a token (method in
/// the high nibble: 0x10 raw, 0x20 LZ4), compressed and original length and a checksum (little-endian i32s; the
/// checksum is XXH32 with [`LZ4_SEED`] masked to 28 bits), until an empty block or the end of the input.
///
/// # Errors
/// A malformed block or a checksum mismatch.
pub fn lz4_stream(data: &[u8]) -> Result<Vec<u8>> {
    const MAGIC: &[u8; 8] = b"LZ4Block";
    let mut out = Vec::new();
    let mut p = 0usize;
    while p < data.len() {
        let h = data.get(p..p + 21).ok_or(CompressError::Truncated)?;
        if &h[..8] != MAGIC {
            return Err(CompressError::Corrupt("LZ4Block magic"));
        }
        let method = h[8] & 0xf0;
        let clen = usize::try_from(le32(&h[9..]) as i32)
            .map_err(|_| CompressError::Corrupt("LZ4 length"))?;
        let olen = usize::try_from(le32(&h[13..]) as i32)
            .map_err(|_| CompressError::Corrupt("LZ4 length"))?;
        let check = le32(&h[17..]);
        p += 21;
        if olen == 0 {
            break;
        }
        if out.len() + olen > MAX_OUTPUT {
            return Err(CompressError::TooLarge);
        }
        let body = data
            .get(p..p.checked_add(clen).ok_or(CompressError::Truncated)?)
            .ok_or(CompressError::Truncated)?;
        let start = out.len();
        match method {
            0x10 => {
                if clen != olen {
                    return Err(CompressError::Corrupt("LZ4 raw block length"));
                }
                out.extend_from_slice(body);
            }
            0x20 => lz4_block(body, &mut out, olen)?,
            _ => return Err(CompressError::Corrupt("LZ4 method")),
        }
        if xxh32(&out[start..], LZ4_SEED) & 0x0FFF_FFFF != check {
            return Err(CompressError::Checksum);
        }
        p += clen;
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn checksums_match_known_values() {
        assert_eq!(crc32(b"123456789"), 0xCBF4_3926);
        assert_eq!(adler32(b"Wikipedia"), 0x11E6_0398);
        assert_eq!(xxh32(b"", 0), 0x02CC_5D05);
        assert_eq!(xxh32(b"abc", 0), 0x32D1_53FF);
        assert_eq!(
            xxh32(b"Nobody inspects the spammish repetition", 0),
            0xE229_3B2F
        );
    }

    #[test]
    fn inflates_stored_and_fixed_blocks() {
        // zlib.compress(b"hello hello hello", 9) and a stored block of "abc".
        let z = [
            0x78, 0xda, 0xcb, 0x48, 0xcd, 0xc9, 0xc9, 0x57, 0xc8, 0x40, 0x90, 0x00, 0x3a, 0x2e,
            0x06, 0x7d,
        ];
        assert_eq!(zlib(&z).unwrap(), b"hello hello hello");
        let stored = [0x01, 0x03, 0x00, 0xfc, 0xff, b'a', b'b', b'c'];
        assert_eq!(inflate(&stored, 100).unwrap(), (b"abc".to_vec(), 8));
    }

    #[test]
    fn rejects_bad_streams() {
        assert!(zlib(&[0x78, 0xda, 0xff, 0xff, 0xff, 0xff]).is_err());
        assert!(inflate(&[0x07], 100).is_err());
        assert!(lz4_stream(b"LZ4Blocx").is_err());
    }

    #[test]
    fn lz4_overlapping_match() {
        // token: 1 literal, match length 4+4=8 at offset 1 -> "aaaaaaaaa"; then a last literal-only sequence.
        let block = [0x14, b'a', 1, 0, 0x10, b'b'];
        let mut out = Vec::new();
        lz4_block(&block, &mut out, 10).unwrap();
        assert_eq!(out, b"aaaaaaaaab");
    }
}
