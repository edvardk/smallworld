//! Python's text rules, so this binary reads, compares and prints text exactly as `mc_rcon.py` does.
//!
//! A Rust `String` is always valid UTF-8. A Python `str` is a sequence of code points that may also hold "lone
//! surrogates" (U+D800 to U+DFFF): Python decodes a command-line argument or an environment variable that is not
//! valid UTF-8 into them (the `surrogateescape` rule), and a `\uD83D` escape in server.properties becomes one.
//! Such a string only fails when it is encoded (sent to the server). To fail at the same moment, with the same
//! message, `PyStr` keeps code points as plain numbers (`u32`) instead of `char`s.
//!
//! Python equivalent: this whole file is what Python gives you for free in `str`.

use crate::error::PyError;
use std::fmt::Write as _;

/// A Python `str`: a sequence of code points, lone surrogates allowed.
///
/// `#[derive(...)]` asks the compiler to write the obvious implementations: `Clone` (`.clone()` makes a copy),
/// `PartialEq`/`Eq` (`==` compares the code points, like Python's `==`), `Hash` (usable as a `HashMap` key, like
/// a Python dict key), `Debug` (`{:?}` printing for tests) and `Default` (the empty string).
#[derive(Clone, Debug, Default, PartialEq, Eq, Hash)]
pub struct PyStr(Vec<u32>);

/// Surrogates are the code points a Rust `char` can't hold.
fn is_surrogate(cp: u32) -> bool {
    (0xD800..=0xDFFF).contains(&cp)
}

/// Python's `str.isspace()` (and what `\s` matches in a `re` pattern, and what `str.strip()` removes).
/// Rust's `char::is_whitespace` is the Unicode `White_Space` property; Python adds the four ASCII "information
/// separators" U+001C to U+001F.
pub fn is_space(cp: u32) -> bool {
    matches!(cp, 0x09..=0x0D | 0x1C..=0x20 | 0x85 | 0xA0 | 0x1680 | 0x2000..=0x200A | 0x2028 | 0x2029)
        || matches!(cp, 0x202F | 0x205F | 0x3000)
}

/// Python's `str.splitlines()` line boundaries (besides `\r\n`, which counts as one).
fn is_line_break(cp: u32) -> bool {
    matches!(cp, 0x0A..=0x0D | 0x1C..=0x1E | 0x85 | 0x2028 | 0x2029)
}

/// Whether `repr()` shows a code point as it is (Python's `str.isprintable()`, for the code points that can
/// matter here). Not printable: control characters, surrogates, private use, the separators except the space,
/// and the common "format" characters. Unassigned code points are not looked up (see docs/rust/smallworld-rcon.md).
fn is_printable(cp: u32) -> bool {
    !(cp < 0x20
        || (0x7F..=0xA0).contains(&cp)
        || is_surrogate(cp)
        || (0xE000..=0xF8FF).contains(&cp)
        || cp >= 0xF0000
        || matches!(cp, 0xAD | 0x061C | 0x070F | 0x180E | 0x1680 | 0xFEFF)
        || (0x0600..=0x0605).contains(&cp)
        || (0x2000..=0x200F).contains(&cp)
        || (0x2028..=0x202F).contains(&cp)
        || (0x205F..=0x2064).contains(&cp)
        || (0x2066..=0x206F).contains(&cp)
        || cp == 0x3000
        || (0xFFF9..=0xFFFB).contains(&cp)
        || (0xE0001..=0xE007F).contains(&cp))
}

/// How one UTF-8 decoding error looks: where it starts, how many bytes it covers, and Python's reason.
struct Utf8Fault {
    start: usize,
    len: usize,
    reason: &'static str,
}

/// The first invalid sequence in `bytes`, split the way both Python's and Rust's decoders split it (the Unicode
/// "maximal subpart" rule), or `None` if all of it is valid UTF-8.
fn first_utf8_fault(bytes: &[u8]) -> Option<Utf8Fault> {
    let err = std::str::from_utf8(bytes).err()?;
    let start = err.valid_up_to();
    Some(match err.error_len() {
        // The input ended in the middle of a sequence.
        None => Utf8Fault {
            start,
            len: bytes.len() - start,
            reason: "unexpected end of data",
        },
        Some(len) => {
            let lead = bytes[start];
            let bad_lead = (0x80..=0xC1).contains(&lead) || lead >= 0xF5;
            let reason = if bad_lead {
                "invalid start byte"
            } else {
                "invalid continuation byte"
            };
            Utf8Fault { start, len, reason }
        }
    })
}

/// `bytes.decode("utf-8")`: the text, or Python's `UnicodeDecodeError`.
pub fn decode_utf8_strict(bytes: &[u8]) -> Result<String, PyError> {
    match first_utf8_fault(bytes) {
        None => Ok(String::from_utf8_lossy(bytes).into_owned()),
        Some(f) => {
            let what = if f.len == 1 {
                format!("byte 0x{:02x} in position {}", bytes[f.start], f.start)
            } else {
                format!("bytes in position {}-{}", f.start, f.start + f.len - 1)
            };
            Err(PyError::UnicodeDecode(format!(
                "'utf-8' codec can't decode {what}: {}",
                f.reason
            )))
        }
    }
}

impl PyStr {
    /// The empty string, `""`.
    pub fn new() -> Self {
        PyStr(Vec::new())
    }

    /// Decodes bytes from the operating system (a command-line argument, an environment variable) the way Python
    /// does on Linux: UTF-8, and each byte of an invalid sequence becomes the lone surrogate U+DC00 + byte.
    pub fn from_os_bytes(mut bytes: &[u8]) -> Self {
        let mut out = PyStr::new();
        while let Some(f) = first_utf8_fault(bytes) {
            out.push_str(&String::from_utf8_lossy(&bytes[..f.start]));
            for b in &bytes[f.start..f.start + f.len] {
                out.0.push(0xDC00 + u32::from(*b));
            }
            bytes = &bytes[f.start + f.len..];
        }
        out.push_str(&String::from_utf8_lossy(bytes));
        out
    }

    /// The reverse of `from_os_bytes`: the bytes to hand back to the operating system (a file path).
    pub fn to_os_bytes(&self) -> Vec<u8> {
        let mut out = Vec::new();
        for &cp in &self.0 {
            match char::from_u32(cp) {
                Some(c) => out.extend_from_slice(c.encode_utf8(&mut [0; 4]).as_bytes()),
                // U+DC80..U+DCFF came from one byte; any other surrogate can't be a path byte: keep a '?'.
                None => out.push(u8::try_from(cp - 0xDC00).unwrap_or(b'?')),
            }
        }
        out
    }

    /// The code points, for code that walks the string.
    pub fn code_points(&self) -> &[u32] {
        &self.0
    }

    /// Builds a string from code points (a slice of another `PyStr`).
    pub fn from_code_points(cps: &[u32]) -> Self {
        PyStr(cps.to_vec())
    }

    pub fn is_empty(&self) -> bool {
        self.0.is_empty()
    }

    pub fn push(&mut self, cp: u32) {
        self.0.push(cp);
    }

    pub fn push_str(&mut self, s: &str) {
        self.0.extend(s.chars().map(u32::from));
    }

    pub fn push_py(&mut self, other: &PyStr) {
        self.0.extend_from_slice(&other.0);
    }

    /// `self == s` for a Rust string literal.
    pub fn eq_str(&self, s: &str) -> bool {
        self.0.iter().copied().eq(s.chars().map(u32::from))
    }

    /// `" ".join(parts)`.
    pub fn join(parts: &[PyStr], sep: &str) -> PyStr {
        let mut out = PyStr::new();
        for (i, p) in parts.iter().enumerate() {
            if i > 0 {
                out.push_str(sep);
            }
            out.push_py(p);
        }
        out
    }

    /// The text as a Rust `String`, if it holds no lone surrogate.
    pub fn to_string_checked(&self) -> Option<String> {
        self.0.iter().map(|&cp| char::from_u32(cp)).collect()
    }

    /// The text as Python writes it to stderr: lone surrogates as `\udcff` (`errors="backslashreplace"`).
    pub fn display(&self) -> String {
        let mut out = String::new();
        for &cp in &self.0 {
            match char::from_u32(cp) {
                Some(c) => out.push(c),
                None => {
                    let _ = write!(out, "\\u{cp:04x}");
                }
            }
        }
        out
    }

    /// `repr(self)`: quoted, with Python's escapes. Single quotes unless the text has a `'` and no `"`.
    pub fn repr(&self) -> String {
        let quote = if self.0.contains(&u32::from('\'')) && !self.0.contains(&u32::from('"')) {
            '"'
        } else {
            '\''
        };
        let mut out = String::new();
        out.push(quote);
        for &cp in &self.0 {
            match char::from_u32(cp) {
                Some(c) if c == quote || c == '\\' => {
                    out.push('\\');
                    out.push(c);
                }
                Some('\t') => out.push_str("\\t"),
                Some('\n') => out.push_str("\\n"),
                Some('\r') => out.push_str("\\r"),
                Some(c) if is_printable(cp) => out.push(c),
                _ if cp <= 0xFF => {
                    let _ = write!(out, "\\x{cp:02x}");
                }
                _ if cp <= 0xFFFF => {
                    let _ = write!(out, "\\u{cp:04x}");
                }
                _ => {
                    let _ = write!(out, "\\U{cp:08x}");
                }
            }
        }
        out.push(quote);
        out
    }

    /// `self.encode("utf-8")`: the bytes, or Python's `UnicodeEncodeError` for a run of lone surrogates.
    pub fn encode_utf8(&self) -> Result<Vec<u8>, PyError> {
        let mut out = Vec::new();
        let mut i = 0;
        while i < self.0.len() {
            let cp = self.0[i];
            if let Some(c) = char::from_u32(cp) {
                out.extend_from_slice(c.encode_utf8(&mut [0; 4]).as_bytes());
                i += 1;
                continue;
            }
            // Python reports the whole run of consecutive surrogates.
            let end = (i..self.0.len())
                .find(|&j| !is_surrogate(self.0[j]))
                .unwrap_or(self.0.len());
            let what = if end == i + 1 {
                format!("character '\\u{cp:04x}' in position {i}")
            } else {
                format!("characters in position {i}-{}", end - 1)
            };
            return Err(PyError::UnicodeEncode(format!(
                "'utf-8' codec can't encode {what}: surrogates not allowed"
            )));
        }
        Ok(out)
    }

    /// `self.startswith(prefix)` for another `PyStr`.
    pub fn starts_with_py(&self, prefix: &PyStr) -> bool {
        self.0.starts_with(&prefix.0)
    }

    /// `self.strip()`: without leading and trailing whitespace (Python's definition, `is_space`).
    pub fn strip(&self) -> PyStr {
        let s = &self.0;
        let start = s.iter().position(|&c| !is_space(c)).unwrap_or(s.len());
        let end = s
            .iter()
            .rposition(|&c| !is_space(c))
            .map_or(start, |i| i + 1);
        PyStr(s[start..end].to_vec())
    }

    /// `self.splitlines()`: lines without their line breaks.
    pub fn splitlines(&self) -> Vec<PyStr> {
        let s = &self.0;
        let mut lines = Vec::new();
        let mut start = 0;
        let mut i = 0;
        while i < s.len() {
            if is_line_break(s[i]) {
                lines.push(PyStr(s[start..i].to_vec()));
                // "\r\n" is one line break.
                i += if s[i] == 0x0D && s.get(i + 1) == Some(&0x0A) {
                    2
                } else {
                    1
                };
                start = i;
            } else {
                i += 1;
            }
        }
        if start < s.len() {
            lines.push(PyStr(s[start..].to_vec()));
        }
        lines
    }
}

/// `PyStr::from("text")`, like writing a literal `"text"` in Python.
impl From<&str> for PyStr {
    fn from(s: &str) -> Self {
        PyStr(s.chars().map(u32::from).collect())
    }
}

/// Python's text conversion `str(int)` stops at 4300 digits (`sys.get_int_max_str_digits()`), both ways.
pub const INT_MAX_STR_DIGITS: usize = 4300;

/// A Python `int`, which has no size limit: a sign and its decimal digits (no leading zeros; zero is "0",
/// never negative). Big enough for anything a command line can say.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PyInt {
    pub negative: bool,
    pub digits: String,
}

/// Why `int(text)` failed.
#[derive(Debug, PartialEq, Eq)]
pub enum IntError {
    /// `ValueError: invalid literal for int() with base 10: '...'`
    Invalid,
    /// `ValueError: Exceeds the limit (4300 digits) ...: value has N digits ...`
    TooManyDigits(usize),
}

impl PyInt {
    /// `int(text)`, Python's rules for base 10: surrounding whitespace, an optional sign, ASCII digits with
    /// single underscores between them. (Python also accepts non-ASCII decimal digits such as `٣`; this doesn't.)
    pub fn parse(text: &PyStr) -> Result<PyInt, IntError> {
        let s = text.strip();
        let (negative, body) = split_sign(s.code_points());
        let digits = digits_with_underscores(body, 10).ok_or(IntError::Invalid)?;
        if digits.len() > INT_MAX_STR_DIGITS {
            return Err(IntError::TooManyDigits(digits.len()));
        }
        Ok(PyInt::from_parts(negative, &digits))
    }

    /// The number from a sign and decimal digits (leading zeros allowed).
    pub fn from_parts(negative: bool, digits: &str) -> PyInt {
        let trimmed = digits.trim_start_matches('0');
        let digits = if trimmed.is_empty() { "0" } else { trimmed }.to_string();
        let negative = negative && digits != "0";
        PyInt { negative, digits }
    }

    pub fn from_i128(n: i128) -> PyInt {
        PyInt::from_parts(n < 0, &n.unsigned_abs().to_string())
    }

    /// The value, if it fits an `i128` (anything a person would type does).
    pub fn to_i128(&self) -> Option<i128> {
        let magnitude: i128 = self.digits.parse().ok()?;
        Some(if self.negative { -magnitude } else { magnitude })
    }

    pub fn is_zero(&self) -> bool {
        self.digits == "0"
    }

    /// `2 * self`, digit by digit (Python ints don't overflow, so neither may this).
    pub fn doubled(&self) -> PyInt {
        let mut out = Vec::with_capacity(self.digits.len() + 1);
        let mut carry = 0;
        for d in self.digits.bytes().rev() {
            let v = (d - b'0') * 2 + carry;
            out.push(b'0' + v % 10);
            carry = v / 10;
        }
        if carry > 0 {
            out.push(b'0' + carry);
        }
        out.reverse();
        PyInt::from_parts(self.negative, &String::from_utf8_lossy(&out))
    }

    /// `str(self)`, or Python's `ValueError` past 4300 digits.
    pub fn to_str(&self) -> Result<String, PyError> {
        if self.digits.len() > INT_MAX_STR_DIGITS {
            return Err(PyError::Value(format!(
                "Exceeds the limit ({INT_MAX_STR_DIGITS} digits) for integer string conversion; use \
                 sys.set_int_max_str_digits() to increase the limit"
            )));
        }
        Ok(format!(
            "{}{}",
            if self.negative { "-" } else { "" },
            self.digits
        ))
    }
}

/// An optional leading `+` or `-`.
fn split_sign(s: &[u32]) -> (bool, &[u32]) {
    match s.first() {
        Some(&c) if c == u32::from('-') => (true, &s[1..]),
        Some(&c) if c == u32::from('+') => (false, &s[1..]),
        _ => (false, s),
    }
}

/// Digits in `base` with single underscores between them (none first or last): the digits without the
/// underscores, or `None`.
fn digits_with_underscores(s: &[u32], base: u32) -> Option<String> {
    let mut out = String::new();
    let mut after_digit = false;
    for &cp in s {
        if cp == u32::from('_') {
            if !after_digit {
                return None;
            }
            after_digit = false;
            continue;
        }
        let c = char::from_u32(cp)?;
        c.to_digit(base)?;
        out.push(c);
        after_digit = true;
    }
    (after_digit && !out.is_empty()).then_some(out)
}

/// `int(text, 16)` for the four characters of a `\uXXXX` escape: whitespace, a sign, an optional `0x` prefix
/// (`_` allowed right after it) and hex digits with single underscores between them.
pub fn parse_hex(text: &[u32]) -> Option<i64> {
    let s = PyStr::from_code_points(text).strip();
    let (negative, mut body) = split_sign(s.code_points());
    let prefixed =
        body.len() >= 2 && body[0] == u32::from('0') && (body[1] | 0x20) == u32::from('x');
    if prefixed {
        body = &body[2..];
        // After a base prefix one underscore may come first: int("0x_1f", 16) == 31.
        if body.first() == Some(&u32::from('_')) {
            body = &body[1..];
        }
    }
    let digits = digits_with_underscores(body, 16)?;
    let value = i64::from_str_radix(&digits, 16).ok()?;
    Some(if negative { -value } else { value })
}

// --- Python's text rules for Rust strings ---------------------------------------------------------------------
//
// The agent works on Rust `String`s (JSON text is always valid Unicode, so lone surrogates can't occur), but every
// length, slice and whitespace rule must still be Python's: lengths and slices count code points (`text[:300]`),
// `str.isspace()` includes U+001C..U+001F (Rust's `char::is_whitespace` doesn't), and `re.ASCII`'s `\d` is 0-9
// only. Never byte-slice or call `char::is_whitespace` where Python semantics matter: use these.

/// `ch.isspace()` for one character (Python's definition: `is_space`).
pub fn isspace(ch: char) -> bool {
    is_space(u32::from(ch))
}

/// `len(text)`: the number of code points.
pub fn len_cp(text: &str) -> usize {
    text.chars().count()
}

/// `text[:n]`: the first `n` code points (all of `text` if it is shorter).
pub fn prefix_cp(text: &str, n: usize) -> &str {
    match text.char_indices().nth(n) {
        Some((i, _)) => &text[..i],
        None => text,
    }
}

/// `text[start:end]` for non-negative bounds (`end` None = to the end), in code points, clamped like Python.
pub fn slice_cp(text: &str, start: usize, end: Option<usize>) -> &str {
    let byte_at = |n: usize| text.char_indices().nth(n).map_or(text.len(), |(i, _)| i);
    let from = byte_at(start);
    let to = end.map_or(text.len(), byte_at);
    if to <= from { "" } else { &text[from..to] }
}

/// `text.split()` (no argument): the words between runs of Python whitespace, no empty words.
pub fn split_ws(text: &str) -> Vec<&str> {
    text.split(isspace).filter(|w| !w.is_empty()).collect()
}

/// `text.strip()` (no argument): without leading and trailing Python whitespace.
pub fn strip_ws(text: &str) -> &str {
    text.trim_matches(isspace)
}

/// `" ".join(text.split())`: whitespace runs collapsed to one space, none at either end.
pub fn collapse_ws(text: &str) -> String {
    split_ws(text).join(" ")
}

/// `ch.isdigit()` under `re.ASCII` (`\d` in a pattern compiled with `re.ASCII`): 0-9 only. Rust's regex `\d` is
/// Unicode (it matches Arabic-Indic digits, which NFKC doesn't fold): write `[0-9]` in patterns.
pub fn is_ascii_digit(ch: char) -> bool {
    ch.is_ascii_digit()
}

/// `text.isdigit()` for a whole string under ASCII rules (non-empty, every character 0-9). Note: Python's own
/// `str.isdigit()` is Unicode; use this only where the Python code checks ASCII digits (a regex with re.ASCII,
/// or text that is ASCII by construction).
pub fn all_ascii_digits(text: &str) -> bool {
    !text.is_empty() && text.chars().all(|c| c.is_ascii_digit())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn s(text: &str) -> PyStr {
        PyStr::from(text)
    }

    #[test]
    fn text_helpers_follow_python() {
        assert!(isspace('\u{1c}') && isspace('\u{85}') && isspace('\u{3000}'));
        assert!(!'\u{1c}'.is_whitespace()); // why isspace exists
        assert!(!isspace('\u{200b}'));
        assert_eq!(len_cp("añ😀"), 3);
        assert_eq!(prefix_cp("añ😀b", 3), "añ😀");
        assert_eq!(prefix_cp("ab", 5), "ab");
        assert_eq!(slice_cp("abcdef", 2, Some(4)), "cd");
        assert_eq!(slice_cp("abc", 5, None), "");
        assert_eq!(slice_cp("abc", 2, Some(1)), "");
        assert_eq!(split_ws(" a\u{1c}b\t\u{a0}c  "), vec!["a", "b", "c"]);
        assert_eq!(strip_ws("\u{1f} x \u{2028}"), "x");
        assert_eq!(collapse_ws("  a   b\nc "), "a b c");
        assert!(!is_ascii_digit('\u{663}') && is_ascii_digit('7'));
        assert!(all_ascii_digits("4671") && !all_ascii_digits("") && !all_ascii_digits("4\u{663}"));
    }

    #[test]
    fn surrogateescape_round_trip() {
        let p = PyStr::from_os_bytes(b"a\xffb\xe2\x82");
        assert_eq!(p.code_points(), &[0x61, 0xDCFF, 0x62, 0xDCE2, 0xDC82]);
        assert_eq!(p.to_os_bytes(), b"a\xffb\xe2\x82");
        assert_eq!(p.display(), "a\\udcffb\\udce2\\udc82");
        assert_eq!(p.repr(), "'a\\udcffb\\udce2\\udc82'");
    }

    #[test]
    fn repr_matches_python() {
        assert_eq!(s("abc").repr(), "'abc'");
        assert_eq!(s("it's").repr(), "\"it's\"");
        assert_eq!(s("it's \"x\"").repr(), "'it\\'s \"x\"'");
        assert_eq!(
            s("a\\b\tc\n\x01\x7f\u{a0}é").repr(),
            "'a\\\\b\\tc\\n\\x01\\x7f\\xa0é'"
        );
        assert_eq!(s("\u{2028}\u{feff}").repr(), "'\\u2028\\ufeff'");
    }

    #[test]
    fn encode_reports_surrogate_runs() {
        assert_eq!(s("ok").encode_utf8().unwrap(), b"ok");
        let one = PyStr::from_os_bytes(b"ab\xff").encode_utf8().unwrap_err();
        assert_eq!(
            one.to_string(),
            "'utf-8' codec can't encode character '\\udcff' in position 2: surrogates not allowed"
        );
        let run = PyStr::from_os_bytes(b"\xff\xfe!")
            .encode_utf8()
            .unwrap_err();
        assert_eq!(
            run.to_string(),
            "'utf-8' codec can't encode characters in position 0-1: surrogates not allowed"
        );
    }

    #[test]
    fn strict_decode_messages() {
        let msg = |b: &[u8]| decode_utf8_strict(b).unwrap_err().to_string();
        assert_eq!(
            msg(b"ab\xff"),
            "'utf-8' codec can't decode byte 0xff in position 2: invalid start byte"
        );
        assert_eq!(
            msg(b"\xe2\x28"),
            "'utf-8' codec can't decode byte 0xe2 in position 0: invalid continuation byte"
        );
        assert_eq!(
            msg(b"\xe2\x82\x28"),
            "'utf-8' codec can't decode bytes in position 0-1: invalid continuation byte"
        );
        assert_eq!(
            msg(b"x\xe2\x82"),
            "'utf-8' codec can't decode bytes in position 1-2: unexpected end of data"
        );
        assert_eq!(decode_utf8_strict("é".as_bytes()).unwrap(), "é");
    }

    #[test]
    fn whitespace_strip_and_lines() {
        assert!(is_space(0x1F) && is_space(0x3000) && !is_space(0x200B));
        assert_eq!(s("\u{1c} a b \u{a0}").strip(), s("a b"));
        let lines = s("a\r\nb\rc\x0bd\u{85}e\n\nf\n").splitlines();
        let want: Vec<PyStr> = ["a", "b", "c", "d", "e", "", "f"]
            .iter()
            .map(|t| s(t))
            .collect();
        assert_eq!(lines, want);
        assert!(s("").splitlines().is_empty());
    }

    #[test]
    fn int_follows_python() {
        let ok = |t: &str| PyInt::parse(&s(t)).unwrap().to_str().unwrap();
        assert_eq!(ok(" 42 "), "42");
        assert_eq!(ok("+5_000"), "5000");
        assert_eq!(ok("-007"), "-7");
        assert_eq!(ok("-0"), "0");
        for bad in [
            "", " ", "1_", "_1", "1__2", "0x5", "1.5", "+-1", "--1", "five",
        ] {
            assert_eq!(PyInt::parse(&s(bad)), Err(IntError::Invalid), "{bad}");
        }
        assert_eq!(
            PyInt::parse(&s(&"9".repeat(4301))),
            Err(IntError::TooManyDigits(4301))
        );
        assert_eq!(PyInt::from_i128(-25).doubled().to_str().unwrap(), "-50");
        assert_eq!(
            PyInt::parse(&s("99999999999999999999999999999999999999999"))
                .unwrap()
                .to_i128(),
            None
        );
        let big = PyInt::parse(&s(&"9".repeat(4300))).unwrap().doubled();
        assert!(big.to_str().is_err());
    }

    #[test]
    fn hex_follows_python() {
        let h = |t: &str| parse_hex(&s(t).0);
        assert_eq!(h("00e9"), Some(0xE9));
        assert_eq!(h("0x41"), Some(0x41));
        assert_eq!(h(" 41 "), Some(0x41));
        assert_eq!(h("0_41"), Some(0x41));
        assert_eq!(h("0x_1"), Some(1));
        assert_eq!(h("-001"), Some(-1));
        assert_eq!(h("ZZZZ"), None);
        assert_eq!(h("0x"), None);
        assert_eq!(h("_041"), None);
    }
}
