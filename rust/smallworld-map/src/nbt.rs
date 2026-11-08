//! A small, read-only, bounds-checked NBT reader (Java edition, big-endian, uncompressed input).
//!
//! The whole payload is parsed into a tree that borrows from the input: strings and arrays are slices of it, so
//! nothing but the compound/list skeleton is allocated. Every length is checked against the bytes that remain
//! before anything is read or allocated, nesting is capped at 512 levels (vanilla's `NbtAccounter` limit), and a
//! malformed input is an `Err`, never a panic (tests/fuzz.rs feeds it random and truncated data).

use std::fmt;

/// Vanilla's maximum nesting depth (`NbtAccounter.DEFAULT_MAX_DEPTH`).
pub const MAX_DEPTH: usize = 512;

/// Why a payload could not be read.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum NbtError {
    /// The input ended inside a tag.
    Truncated,
    /// An unknown tag id.
    BadTag(u8),
    /// A negative or impossible length.
    BadLength,
    /// Deeper than [`MAX_DEPTH`].
    TooDeep,
    /// The root is not a compound.
    NotCompound,
}

impl fmt::Display for NbtError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            NbtError::Truncated => write!(f, "truncated NBT"),
            NbtError::BadTag(t) => write!(f, "unknown NBT tag {t}"),
            NbtError::BadLength => write!(f, "bad NBT length"),
            NbtError::TooDeep => write!(f, "NBT nested deeper than {MAX_DEPTH}"),
            NbtError::NotCompound => write!(f, "NBT root is not a compound"),
        }
    }
}

impl std::error::Error for NbtError {}

/// One NBT value. Strings are the raw (modified UTF-8) bytes; arrays are the raw big-endian bytes.
#[derive(Debug, Clone, PartialEq)]
pub enum Value<'a> {
    Byte(i8),
    Short(i16),
    Int(i32),
    Long(i64),
    Float(f32),
    Double(f64),
    ByteArray(&'a [u8]),
    String(&'a [u8]),
    List(Vec<Value<'a>>),
    Compound(Compound<'a>),
    IntArray(&'a [u8]),
    LongArray(&'a [u8]),
}

/// A compound's entries in file order (names are raw bytes).
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Compound<'a> {
    pub entries: Vec<(&'a [u8], Value<'a>)>,
}

impl<'a> Compound<'a> {
    /// The value named `name` (the first one, like vanilla's map would keep the last: duplicates don't occur in
    /// files the game writes).
    #[must_use]
    pub fn get(&self, name: &str) -> Option<&Value<'a>> {
        self.entries
            .iter()
            .find(|(k, _)| *k == name.as_bytes())
            .map(|(_, v)| v)
    }

    #[must_use]
    pub fn int(&self, name: &str) -> Option<i32> {
        match self.get(name)? {
            Value::Int(v) => Some(*v),
            Value::Short(v) => Some(i32::from(*v)),
            Value::Byte(v) => Some(i32::from(*v)),
            _ => None,
        }
    }

    #[must_use]
    pub fn string(&self, name: &str) -> Option<&'a [u8]> {
        match self.get(name)? {
            Value::String(s) => Some(s),
            _ => None,
        }
    }

    #[must_use]
    pub fn compound(&self, name: &str) -> Option<&Compound<'a>> {
        match self.get(name)? {
            Value::Compound(c) => Some(c),
            _ => None,
        }
    }

    #[must_use]
    pub fn list(&self, name: &str) -> Option<&[Value<'a>]> {
        match self.get(name)? {
            Value::List(l) => Some(l),
            _ => None,
        }
    }

    /// A long array's raw bytes (8 per value, big-endian).
    #[must_use]
    pub fn long_array(&self, name: &str) -> Option<&'a [u8]> {
        match self.get(name)? {
            Value::LongArray(a) => Some(a),
            _ => None,
        }
    }
}

/// The `i`-th value of a long array's raw bytes.
#[must_use]
pub fn long_at(raw: &[u8], i: usize) -> u64 {
    let b = &raw[i * 8..i * 8 + 8];
    u64::from_be_bytes([b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7]])
}

struct Reader<'a> {
    data: &'a [u8],
    pos: usize,
}

impl<'a> Reader<'a> {
    fn take(&mut self, n: usize) -> Result<&'a [u8], NbtError> {
        let end = self.pos.checked_add(n).ok_or(NbtError::Truncated)?;
        let s = self.data.get(self.pos..end).ok_or(NbtError::Truncated)?;
        self.pos = end;
        Ok(s)
    }

    fn remaining(&self) -> usize {
        self.data.len() - self.pos
    }

    fn u8(&mut self) -> Result<u8, NbtError> {
        Ok(self.take(1)?[0])
    }

    fn u16(&mut self) -> Result<u16, NbtError> {
        let b = self.take(2)?;
        Ok(u16::from_be_bytes([b[0], b[1]]))
    }

    fn i32(&mut self) -> Result<i32, NbtError> {
        let b = self.take(4)?;
        Ok(i32::from_be_bytes([b[0], b[1], b[2], b[3]]))
    }

    fn i64(&mut self) -> Result<i64, NbtError> {
        let b = self.take(8)?;
        Ok(i64::from_be_bytes([
            b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7],
        ]))
    }

    fn string(&mut self) -> Result<&'a [u8], NbtError> {
        let n = self.u16()?;
        self.take(usize::from(n))
    }

    /// An array length (a signed int) times the element size, checked against what is left.
    fn array(&mut self, elem: usize) -> Result<&'a [u8], NbtError> {
        let n = usize::try_from(self.i32()?).map_err(|_| NbtError::BadLength)?;
        let bytes = n.checked_mul(elem).ok_or(NbtError::BadLength)?;
        if bytes > self.remaining() {
            return Err(NbtError::Truncated);
        }
        self.take(bytes)
    }

    fn payload(&mut self, tag: u8, depth: usize) -> Result<Value<'a>, NbtError> {
        Ok(match tag {
            1 => Value::Byte(self.u8()? as i8),
            2 => Value::Short(self.u16()? as i16),
            3 => Value::Int(self.i32()?),
            4 => Value::Long(self.i64()?),
            5 => Value::Float(f32::from_bits(self.i32()? as u32)),
            6 => Value::Double(f64::from_bits(self.i64()? as u64)),
            7 => Value::ByteArray(self.array(1)?),
            8 => Value::String(self.string()?),
            9 => {
                if depth >= MAX_DEPTH {
                    return Err(NbtError::TooDeep);
                }
                let elem = self.u8()?;
                let n = usize::try_from(self.i32()?).map_err(|_| NbtError::BadLength)?;
                if elem == 0 {
                    // Vanilla writes an empty list with element type END; a non-empty one is malformed.
                    return if n == 0 {
                        Ok(Value::List(Vec::new()))
                    } else {
                        Err(NbtError::BadLength)
                    };
                }
                if elem > 12 {
                    return Err(NbtError::BadTag(elem));
                }
                // Every element takes at least one byte (a compound's END), most more: refuse a count that can't
                // fit before allocating for it.
                if n > self.remaining() {
                    return Err(NbtError::Truncated);
                }
                let mut out = Vec::with_capacity(n);
                for _ in 0..n {
                    out.push(self.payload(elem, depth + 1)?);
                }
                Value::List(out)
            }
            10 => {
                if depth >= MAX_DEPTH {
                    return Err(NbtError::TooDeep);
                }
                Value::Compound(self.compound(depth + 1)?)
            }
            11 => Value::IntArray(self.array(4)?),
            12 => Value::LongArray(self.array(8)?),
            t => return Err(NbtError::BadTag(t)),
        })
    }

    fn compound(&mut self, depth: usize) -> Result<Compound<'a>, NbtError> {
        let mut entries = Vec::new();
        loop {
            let tag = self.u8()?;
            if tag == 0 {
                return Ok(Compound { entries });
            }
            let name = self.string()?;
            let value = self.payload(tag, depth)?;
            entries.push((name, value));
        }
    }
}

/// Parses a named root compound (what a region file's chunk payload holds once decompressed).
///
/// # Errors
/// Any malformed, truncated or too deeply nested input.
pub fn parse(data: &[u8]) -> Result<Compound<'_>, NbtError> {
    let mut r = Reader { data, pos: 0 };
    if r.u8()? != 10 {
        return Err(NbtError::NotCompound);
    }
    r.string()?;
    r.compound(1)
}

/// Unwraps a 26.x heterogeneous-list element: a list that mixes types stores every element as a compound and wraps
/// a non-compound value `v` as `{"": v}` (`ListTag.wrapElement`).
#[must_use]
pub fn unwrap_list_element<'v, 'a>(v: &'v Value<'a>) -> &'v Value<'a> {
    if let Value::Compound(c) = v
        && c.entries.len() == 1
        && c.entries[0].0.is_empty()
    {
        return &c.entries[0].1;
    }
    v
}

#[cfg(test)]
mod tests {
    use super::*;

    fn named(tag: u8, name: &str, payload: &[u8]) -> Vec<u8> {
        let mut v = vec![tag];
        v.extend_from_slice(&(name.len() as u16).to_be_bytes());
        v.extend_from_slice(name.as_bytes());
        v.extend_from_slice(payload);
        v
    }

    #[test]
    fn parses_nested_values() {
        let mut body = named(3, "DataVersion", &5023i32.to_be_bytes());
        let mut s = 4u16.to_be_bytes().to_vec();
        s.extend_from_slice(b"full");
        body.extend(named(8, "Status", &s));
        let mut longs = 2i32.to_be_bytes().to_vec();
        longs.extend_from_slice(&1u64.to_be_bytes());
        longs.extend_from_slice(&u64::MAX.to_be_bytes());
        body.extend(named(12, "L", &longs));
        body.push(0);
        let data = named(10, "", &body);
        let root = parse(&data).unwrap();
        assert_eq!(root.int("DataVersion"), Some(5023));
        assert_eq!(root.string("Status"), Some(&b"full"[..]));
        let l = root.long_array("L").unwrap();
        assert_eq!(long_at(l, 1), u64::MAX);
    }

    #[test]
    fn refuses_bad_input() {
        assert_eq!(parse(&[]), Err(NbtError::Truncated));
        assert_eq!(parse(&[8, 0, 0]), Err(NbtError::NotCompound));
        // A list claiming 2^31-1 ints with no data.
        let mut body = named(9, "x", &[3, 0x7f, 0xff, 0xff, 0xff]);
        body.push(0);
        assert_eq!(parse(&named(10, "", &body)), Err(NbtError::Truncated));
        // A non-empty list of END.
        let mut body = named(9, "x", &[0, 0, 0, 0, 5]);
        body.push(0);
        assert_eq!(parse(&named(10, "", &body)), Err(NbtError::BadLength));
    }

    #[test]
    fn depth_is_capped() {
        // 600 nested lists of lists.
        let mut data = named(10, "", &[]);
        data.extend(named(9, "x", &[]));
        for _ in 0..600 {
            data.extend_from_slice(&[9, 0, 0, 0, 1]);
        }
        data.extend_from_slice(&[0, 0, 0, 0, 0]);
        assert_eq!(parse(&data), Err(NbtError::TooDeep));
    }
}
